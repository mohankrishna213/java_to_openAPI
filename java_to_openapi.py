"""Convert Spring Boot controller interfaces into OpenAPI specs.

This script can operate in two modes:

* **Single-file mode** (legacy): provide a path to a Java source file.
  Example: ``python java_to_openapi.py MyController.java``

* **Project mode** (new): point at a Spring Boot project root containing a
  ``pom.xml``. The tool will scan ``src/main/java`` (and other standard
  locations) for Java files that contain Spring Web annotations and merge all
  discovered endpoints into one OpenAPI 3.0 document. You can also filter
  packages with ``--include-packages``/``--exclude-packages``.

When run with no arguments the script interactively asks for the project path.
The generated YAML is written to ``<project_root>/openapi-spec/openapi.yaml``
by default.
"""

import argparse
import os
import re
import sys

import javalang
import yaml

def extract_annotation_value(annotation, name=None):
    """Extracts the string (or list of strings) value for a given attribute from a Java annotation.
    If name is None, returns the first literal value found (useful for single-value annotations).
    """
    if not annotation.element:
        return None

    # helper to pull literal
    def _literal_value(v):
        if isinstance(v, javalang.tree.Literal):
            return v.value.strip('"')
        if isinstance(v, javalang.tree.MemberReference):
            # enum reference like RequestMethod.GET
            return v.member
        if isinstance(v, javalang.tree.ArrayInitializer):
            vals = []
            for init in v.initializers:
                if isinstance(init, javalang.tree.Literal):
                    vals.append(init.value.strip('"'))
                elif isinstance(init, javalang.tree.MemberReference):
                    vals.append(init.member)
            return vals
        return None

    # single-element annotation: @GetMapping("/path")
    if isinstance(annotation.element, javalang.tree.Literal):
        if name is None:
            return _literal_value(annotation.element)
        else:
            return None

    # multiple name=value pairs
    if isinstance(annotation.element, list):
        for pair in annotation.element:
            if name is None or pair.name == name:
                val = _literal_value(pair.value)
                if val is not None:
                    return val
    return None


# --- project scanning helpers ------------------------------------------------

def get_package_name(java_code: str) -> str:
    """Return the package name declared in a Java source file (or empty string)."""
    for line in java_code.splitlines():
        line = line.strip()
        if line.startswith("package "):
            return line[len("package "):].rstrip(";")
    return ""


def has_spring_annotations(java_code: str) -> bool:
    """Quick check for Spring web mapping annotations in the source.
    Used to skip files that are clearly unrelated.
    """
    for ann in ["@RequestMapping", "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping"]:
        if ann in java_code:
            return True
    return False


def matches_package_filter(pkg: str, includes, excludes) -> bool:
    """Determine whether a package name passes include/exclude filters."""
    if includes:
        if not any(pkg.startswith(pattern) for pattern in includes):
            return False
    if excludes:
        if any(pkg.startswith(pattern) for pattern in excludes):
            return False
    return True


def find_source_directory(project_root: str) -> str:
    """Auto-detect a Java source directory under the project root."""
    candidates = [
        os.path.join(project_root, "src", "main", "java"),
        os.path.join(project_root, "src"),
        os.path.join(project_root, "java"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return project_root


def discover_java_files(src_dir: str, includes=None, excludes=None) -> list:
    """Recursively find .java files that meet annotation and package filters."""
    result = []
    for root, _, files in os.walk(src_dir):
        for fn in files:
            if not fn.endswith(".java"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    code = f.read()
            except Exception:
                continue
            if not has_spring_annotations(code):
                continue
            pkg = get_package_name(code)
            if matches_package_filter(pkg, includes, excludes):
                result.append(path)
    return result


def extract_maven_metadata(pom_path: str) -> dict:
    """Return artifactId and version extracted from a pom.xml."""
    meta = {"title": "Generated API", "version": "1.0.0"}
    try:
        text = open(pom_path, 'r', encoding='utf-8').read()
    except Exception:
        return meta
    art = re.search(r"<artifactId>([^<]+)</artifactId>", text)
    ver = re.search(r"<version>([^<]+)</version>", text)
    if art:
        meta['title'] = art.group(1)
    if ver:
        meta['version'] = ver.group(1)
    return meta


# internal helper that extracts path definitions from a single Java file

def parse_java_file(java_file_path: str) -> dict:
    """Parse the provided Java source file and return a dict of paths suitable
    for merging into an OpenAPI spec.

    The structure mirrors the ``paths`` object of an OpenAPI document.
    """
    with open(java_file_path, 'r', encoding='utf-8') as file:
        java_code = file.read()

    tree = javalang.parse.parse(java_code)

    paths = {}

    # Map Spring annotations to HTTP methods
    http_methods_map = {
        'GetMapping': 'get',
        'PostMapping': 'post',
        'PutMapping': 'put',
        'DeleteMapping': 'delete',
        'PatchMapping': 'patch'
    }

    # walk through top‑level type declarations and handle both classes and interfaces
    for _, node in tree.filter(javalang.tree.TypeDeclaration):
        if not isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
            continue
        has_mapping = False
        base_path = ""

        for annotation in node.annotations:
            if annotation.name == 'RequestMapping':
                has_mapping = True
                base_path = (extract_annotation_value(annotation)
                             or extract_annotation_value(annotation, 'value')
                             or extract_annotation_value(annotation, 'path')
                             or '')
            elif annotation.name in http_methods_map:
                has_mapping = True
                base_path = extract_annotation_value(annotation) or ''

        for method in node.methods:
            http_verb = None
            endpoint_path = ''
            for annotation in method.annotations:
                if annotation.name in http_methods_map:
                    http_verb = http_methods_map[annotation.name]
                    endpoint_path = extract_annotation_value(annotation) or ''
                    has_mapping = True
                    break
                if annotation.name == 'RequestMapping':
                    has_mapping = True
                    endpoint_path = extract_annotation_value(annotation, 'value') or extract_annotation_value(annotation, 'path') or ''
                    method_val = extract_annotation_value(annotation, 'method')
                    if method_val:
                        methods = method_val if isinstance(method_val, list) else [method_val]
                        for m in methods:
                            verb = m.lower()
                            if verb.startswith('requestmethod.'):
                                verb = verb.split('.',1)[1]
                            if verb in http_methods_map.values():
                                http_verb = verb
                                break
                    break
            if not http_verb:
                continue

            def _join(p1, p2):
                if not p1:
                    return p2 or ''
                if not p2:
                    return p1
                if p1.endswith('/') and p2.startswith('/'):
                    return p1[:-1] + p2
                if not p1.endswith('/') and not p2.startswith('/'):
                    return p1 + '/' + p2
                return p1 + p2

            full_path = _join(base_path, endpoint_path)
            if not full_path.startswith('/'):
                full_path = '/' + full_path

            paths.setdefault(full_path, {})

            parameters = []
            request_body = None
            for param in method.parameters:
                param_name = param.name
                param_type = None
                if hasattr(param.type, 'name'):
                    param_type = param.type.name
                elif hasattr(param.type, 'pattern_type'):
                    param_type = param.type.pattern_type

                if param_type in ["Principal", "Authentication", "HttpServletRequest"]:
                    continue

                openapi_type = "string"
                if param_type in ["int", "Integer", "long", "Long", "short", "Short"]:
                    openapi_type = "integer"
                elif param_type in ["boolean", "Boolean"]:
                    openapi_type = "boolean"
                elif param_type in ["double", "float", "Float", "Double"]:
                    openapi_type = "number"
                elif param_type == "String":
                    openapi_type = "string"
                elif param_type and param_type.endswith("[]"):
                    openapi_type = "array"
                else:
                    openapi_type = "object"

                in_type = 'query'
                required = False
                for pann in param.annotations:
                    if pann.name == 'PathVariable':
                        in_type = 'path'
                        required = True
                        name_override = extract_annotation_value(pann, 'name') or extract_annotation_value(pann, 'value')
                        if name_override:
                            param_name = name_override
                    elif pann.name == 'RequestParam':
                        in_type = 'query'
                        required = True
                        req_val = extract_annotation_value(pann, 'required')
                        if isinstance(req_val, str) and req_val.lower() == 'false':
                            required = False
                        if extract_annotation_value(pann, 'defaultValue') is not None:
                            required = False
                    elif pann.name == 'RequestHeader':
                        in_type = 'header'
                    elif pann.name == 'RequestBody':
                        in_type = None
                        request_body = {
                            "description": f"{param_name} payload",
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": openapi_type
                                    }
                                }
                            }
                        }
                if in_type:
                    parameters.append({
                        "name": param_name,
                        "in": in_type,
                        "required": required,
                        "schema": {"type": openapi_type}
                    })

            # ensure any path segments have corresponding parameters
            path_params = re.findall(r"\{([^/}]+)\}", full_path)
            for pname in path_params:
                found = False
                for p in parameters:
                    if p.get('name') == pname:
                        # normalize to path
                        p['in'] = 'path'
                        p['required'] = True
                        found = True
                        break
                if not found:
                    parameters.append({
                        "name": pname,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"}
                    })

            operation_obj = {
                "summary": method.name,
                "operationId": method.name,
                "parameters": parameters,
                "responses": {"200": {"description": "Successful operation"}}
            }
            if request_body:
                operation_obj["requestBody"] = request_body

            paths[full_path][http_verb] = operation_obj

    return paths


# small wrapper retained for backwards compatibility

def generate_openapi_from_java(java_file_path):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Generated API from Java Source", "version": "1.0.0"},
        "paths": {}
    }
    fragment = parse_java_file(java_file_path)
    spec["paths"].update(fragment)
    return spec


# helper to merge path fragments into a shared spec

def merge_paths(spec_paths: dict, fragment: dict):
    for path, ops in fragment.items():
        if path not in spec_paths:
            spec_paths[path] = ops
        else:
            # merge method operations, last wins if duplicated
            spec_paths[path].update(ops)


def generate_openapi_from_project(project_root: str,
                                   include_packages=None,
                                   exclude_packages=None) -> tuple:
    """Scan an entire Spring Boot project and return a combined OpenAPI spec plus
    list of files that contributed to it."""
    if include_packages:
        include_packages = [p.strip() for p in include_packages if p.strip()]
    if exclude_packages:
        exclude_packages = [p.strip() for p in exclude_packages if p.strip()]

    src = find_source_directory(project_root)
    java_files = discover_java_files(src, include_packages, exclude_packages)

    spec = {"openapi": "3.0.0",
            "info": {"title": "Generated API from Java Source", "version": "1.0.0"},
            "paths": {}}

    for jf in java_files:
        fragment = parse_java_file(jf)
        merge_paths(spec["paths"], fragment)
    return spec, java_files



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert Spring Boot project controllers into an OpenAPI YAML file."
    )
    parser.add_argument('project_root', nargs='?',
                        help='Root directory of the Spring Boot application')
    parser.add_argument('--include-packages',
                        help='Comma-separated list of package prefixes to include')
    parser.add_argument('--exclude-packages',
                        help='Comma-separated list of package prefixes to exclude')
    parser.add_argument('--output-dir', help='Directory in which to write the OpenAPI spec')

    args = parser.parse_args()

    root = args.project_root
    if not root:
        root = input("Enter Spring Boot project root path: ").strip()
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"Invalid project root: {root}")
        sys.exit(1)

    pom = os.path.join(root, 'pom.xml')
    if not os.path.exists(pom):
        print(f"No pom.xml found in {root}; please point at a Maven project")
        sys.exit(1)

    meta = extract_maven_metadata(pom)
    spec, processed_files = generate_openapi_from_project(
        root,
        include_packages=(args.include_packages or '').split(',') if args.include_packages else None,
        exclude_packages=(args.exclude_packages or '').split(',') if args.exclude_packages else None
    )
    spec['info']['title'] = meta.get('title', spec['info']['title'])
    spec['info']['version'] = meta.get('version', spec['info']['version'])

    yaml_output = yaml.dump(spec, sort_keys=False, default_flow_style=False)
    print(yaml_output)

    out_folder = args.output_dir or os.path.join(root, 'openapi-spec')
    os.makedirs(out_folder, exist_ok=True)
    out_path = os.path.join(out_folder, 'openapi.yaml')
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(yaml_output)
        print(f"OpenAPI spec written to {out_path}")
        print(f"Processed {len(processed_files)} controller files")
    except Exception as e:
        print(f"Failed to write spec: {e}")