"""Convert Spring Boot controller interfaces into OpenAPI specs.

This script can operate in two modes:

* **Single-file mode** (legacy): provide a path to a Java source file.
  Example: ``python java_to_openapi.py MyController.java``

* **Project mode** (new): point at a Spring Boot project root containing a
  ``pom.xml``. The tool will scan ``src/main/java`` (and other standard
  locations) for Java files that contain Spring Web annotations and merge all
  discovered endpoints into one OpenAPI 3.0 document. You can also filter
  packages with ``--include-packages``/``--exclude-packages``.

NEW: The script also scans for DTO/model classes and emits a
``components/schemas`` block in the generated spec. It reads Bean Validation
annotations (@NotNull, @NotBlank, @Size, @Min, @Max, @Pattern, @Email,
@Positive, @Negative, @Digits, @DecimalMin, @DecimalMax) and maps them to
OpenAPI 3.0 JSON Schema constraint keywords.

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


# ---------------------------------------------------------------------------
# Annotation value extraction
# ---------------------------------------------------------------------------

def extract_annotation_value(annotation, name=None):
    """Extracts the string (or list of strings) value for a given attribute
    from a Java annotation.

    If *name* is None the first literal value found is returned — useful for
    single-value annotations such as @GetMapping("/path").
    """
    if not annotation.element:
        return None

    def _literal_value(v):
        """Recursively pull a Python value out of a javalang AST value node."""
        if isinstance(v, javalang.tree.Literal):
            raw = v.value.strip('"')
            # Try to coerce numeric literals so callers get ints/floats when useful.
            try:
                return int(raw)
            except ValueError:
                pass
            try:
                return float(raw)
            except ValueError:
                pass
            return raw
        if isinstance(v, javalang.tree.MemberReference):
            # e.g. RequestMethod.GET  →  "GET"
            return v.member
        if isinstance(v, javalang.tree.ArrayInitializer):
            vals = []
            for init in v.initializers:
                lit = _literal_value(init)
                if lit is not None:
                    vals.append(lit)
            return vals
        return None

    # single-element annotation: @GetMapping("/path")
    if isinstance(annotation.element, javalang.tree.Literal):
        if name is None:
            return _literal_value(annotation.element)
        return None

    # named attribute list: @RequestMapping(value="/path", method=GET)
    if isinstance(annotation.element, list):
        for pair in annotation.element:
            if name is None or pair.name == name:
                val = _literal_value(pair.value)
                if val is not None:
                    return val
    return None


# ---------------------------------------------------------------------------
# Project scanning helpers
# ---------------------------------------------------------------------------

def get_package_name(java_code: str) -> str:
    """Return the package name declared in a Java source file (or empty str)."""
    for line in java_code.splitlines():
        line = line.strip()
        if line.startswith("package "):
            return line[len("package "):].rstrip(";").strip()
    return ""


def has_spring_annotations(java_code: str) -> bool:
    """Quick text-scan for Spring web mapping annotations.

    Used to skip files that are clearly not controllers before invoking the
    slower javalang parser.
    """
    for ann in ["@RequestMapping", "@GetMapping", "@PostMapping",
                "@PutMapping", "@DeleteMapping", "@PatchMapping"]:
        if ann in java_code:
            return True
    return False


def has_validation_annotations(java_code: str) -> bool:
    """Quick text-scan for Bean Validation annotations.

    Used to identify DTO / model files that carry field-level constraints.
    """
    for ann in ["@NotNull", "@NotBlank", "@NotEmpty", "@Size", "@Min",
                "@Max", "@Pattern", "@Email", "@Positive", "@Negative",
                "@Digits", "@DecimalMin", "@DecimalMax", "@Valid"]:
        if ann in java_code:
            return True
    return False


def matches_package_filter(pkg: str, includes, excludes) -> bool:
    """Determine whether *pkg* passes the include / exclude prefix filters."""
    if includes:
        if not any(pkg.startswith(p) for p in includes):
            return False
    if excludes:
        if any(pkg.startswith(p) for p in excludes):
            return False
    return True


def find_source_directory(project_root: str) -> str:
    """Auto-detect the Java source root under *project_root*."""
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
    """Recursively find .java files that are Spring controllers."""
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


def discover_model_files(src_dir: str, includes=None, excludes=None) -> list:
    """Recursively find .java files that look like DTOs / model classes.

    A file qualifies if it contains Bean Validation annotations but is *not*
    already a Spring controller (to avoid double-processing).
    """
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
            # Must have validation annotations but need not be a controller.
            if not has_validation_annotations(code):
                continue
            # Skip files that are Spring controllers — they are handled separately.
            if has_spring_annotations(code):
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


# ---------------------------------------------------------------------------
# Java type → OpenAPI type mapping
# ---------------------------------------------------------------------------

# Primitive and common Java types → OpenAPI scalar types
_JAVA_TYPE_MAP = {
    "int":       {"type": "integer", "format": "int32"},
    "Integer":   {"type": "integer", "format": "int32"},
    "long":      {"type": "integer", "format": "int64"},
    "Long":      {"type": "integer", "format": "int64"},
    "short":     {"type": "integer"},
    "Short":     {"type": "integer"},
    "byte":      {"type": "integer"},
    "Byte":      {"type": "integer"},
    "double":    {"type": "number",  "format": "double"},
    "Double":    {"type": "number",  "format": "double"},
    "float":     {"type": "number",  "format": "float"},
    "Float":     {"type": "number",  "format": "float"},
    "boolean":   {"type": "boolean"},
    "Boolean":   {"type": "boolean"},
    "String":    {"type": "string"},
    "UUID":      {"type": "string",  "format": "uuid"},
    "Date":      {"type": "string",  "format": "date"},
    "LocalDate": {"type": "string",  "format": "date"},
    "LocalDateTime": {"type": "string", "format": "date-time"},
    "ZonedDateTime": {"type": "string", "format": "date-time"},
    "OffsetDateTime": {"type": "string", "format": "date-time"},
    "BigDecimal": {"type": "number"},
    "BigInteger": {"type": "integer"},
}

# Collection-like types that translate to OpenAPI array
_COLLECTION_TYPES = {"List", "ArrayList", "Set", "HashSet",
                     "LinkedList", "Collection", "Iterable"}


def java_type_to_schema(type_node) -> dict:
    """Convert a javalang type node into an OpenAPI schema snippet.

    Returns a dict like {"type": "string"} or {"$ref": "#/components/schemas/Foo"}.
    For generic collections (List<Foo>) it returns an array schema whose items
    reference the element type.
    """
    if type_node is None:
        return {"type": "string"}

    type_name = getattr(type_node, 'name', None)

    # Arrays (String[], int[])
    if hasattr(type_node, 'dimensions') and type_node.dimensions:
        inner = {"type": "string"}
        if type_name and type_name in _JAVA_TYPE_MAP:
            inner = dict(_JAVA_TYPE_MAP[type_name])
        return {"type": "array", "items": inner}

    # Generic types: List<Foo>, Set<Bar>
    if type_name in _COLLECTION_TYPES:
        args = getattr(type_node, 'arguments', None)
        if args:
            # arguments is a list of TypeArgument nodes
            first_arg = args[0]
            inner_type = getattr(first_arg, 'type', None)
            if inner_type:
                return {"type": "array", "items": java_type_to_schema(inner_type)}
        return {"type": "array", "items": {"type": "object"}}

    # Map types — represent as object with additionalProperties
    if type_name in ("Map", "HashMap", "LinkedHashMap", "TreeMap"):
        return {"type": "object", "additionalProperties": True}

    # Known scalar types
    if type_name and type_name in _JAVA_TYPE_MAP:
        return dict(_JAVA_TYPE_MAP[type_name])

    # Unknown / custom class → emit a $ref so tools know this is a schema reference
    if type_name:
        return {"$ref": f"#/components/schemas/{type_name}"}

    return {"type": "object"}


# ---------------------------------------------------------------------------
# Bean Validation annotation → OpenAPI constraint keyword mapping
# ---------------------------------------------------------------------------

def apply_validation_annotations(field_annotations, schema: dict) -> list:
    """Read Bean Validation annotations on a field and add constraint keywords
    to *schema* in place.

    Returns a list containing the field name if it is required (i.e. annotated
    with @NotNull, @NotBlank, or @NotEmpty), otherwise an empty list.
    """
    required = False

    for ann in field_annotations:
        name = ann.name

        # ---- Presence constraints → required in OpenAPI ----
        if name in ("NotNull", "NotBlank", "NotEmpty"):
            required = True

        # ---- @Size(min=X, max=Y) ----
        # For strings this means minLength/maxLength.
        # For array types it means minItems/maxItems.
        elif name == "Size":
            min_val = extract_annotation_value(ann, 'min')
            max_val = extract_annotation_value(ann, 'max')
            # Decide which keywords to use based on the current schema type.
            if schema.get("type") == "array":
                if min_val is not None:
                    schema["minItems"] = int(min_val)
                if max_val is not None:
                    schema["maxItems"] = int(max_val)
            else:
                # Default: treat as string length constraint.
                if min_val is not None:
                    schema["minLength"] = int(min_val)
                if max_val is not None:
                    schema["maxLength"] = int(max_val)

        # ---- @Min(value) / @Max(value) → minimum / maximum ----
        elif name == "Min":
            val = (extract_annotation_value(ann) or
                   extract_annotation_value(ann, 'value'))
            if val is not None:
                schema["minimum"] = int(val)

        elif name == "Max":
            val = (extract_annotation_value(ann) or
                   extract_annotation_value(ann, 'value'))
            if val is not None:
                schema["maximum"] = int(val)

        # ---- @DecimalMin / @DecimalMax ----
        elif name == "DecimalMin":
            val = (extract_annotation_value(ann) or
                   extract_annotation_value(ann, 'value'))
            if val is not None:
                schema["minimum"] = float(val)
            inclusive = extract_annotation_value(ann, 'inclusive')
            if isinstance(inclusive, str) and inclusive.lower() == 'false':
                schema["exclusiveMinimum"] = True

        elif name == "DecimalMax":
            val = (extract_annotation_value(ann) or
                   extract_annotation_value(ann, 'value'))
            if val is not None:
                schema["maximum"] = float(val)
            inclusive = extract_annotation_value(ann, 'inclusive')
            if isinstance(inclusive, str) and inclusive.lower() == 'false':
                schema["exclusiveMaximum"] = True

        # ---- @Positive / @PositiveOrZero / @Negative / @NegativeOrZero ----
        elif name == "Positive":
            schema["minimum"] = 0
            schema["exclusiveMinimum"] = True
        elif name == "PositiveOrZero":
            schema["minimum"] = 0
        elif name == "Negative":
            schema["maximum"] = 0
            schema["exclusiveMaximum"] = True
        elif name == "NegativeOrZero":
            schema["maximum"] = 0

        # ---- @Digits(integer=X, fraction=Y) ----
        # Approximate with a pattern constraint.
        elif name == "Digits":
            int_val = extract_annotation_value(ann, 'integer') or 0
            frac_val = extract_annotation_value(ann, 'fraction') or 0
            if frac_val:
                schema["pattern"] = (
                    rf"^\d{{1,{int_val}}}(\.\d{{1,{frac_val}}})?$"
                )
            else:
                schema["pattern"] = rf"^\d{{1,{int_val}}}$"

        # ---- @Pattern(regexp="...") ----
        elif name == "Pattern":
            regexp = (extract_annotation_value(ann, 'regexp') or
                      extract_annotation_value(ann))
            if regexp:
                schema["pattern"] = regexp

        # ---- @Email ----
        elif name == "Email":
            schema["format"] = "email"

    return [True] if required else []


# ---------------------------------------------------------------------------
# Model / DTO parsing  →  components/schemas
# ---------------------------------------------------------------------------

def parse_java_model_file(java_file_path: str) -> dict:
    """Parse a Java DTO / model class and return a components/schemas fragment.

    For each top-level class in the file a schema object is built from its
    declared fields and their Bean Validation annotations.

    Returns a dict  {ClassName: {schema object}, ...}.
    """
    with open(java_file_path, 'r', encoding='utf-8') as f:
        java_code = f.read()

    try:
        tree = javalang.parse.parse(java_code)
    except Exception:
        # Unparseable file (syntax error, unsupported syntax, etc.) → skip silently.
        return {}

    schemas = {}

    for _, node in tree.filter(javalang.tree.TypeDeclaration):
        # Only process concrete classes, not interfaces or enums (for now).
        if not isinstance(node, javalang.tree.ClassDeclaration):
            continue

        # Skip abstract classes — they're rarely DTOs.
        if 'abstract' in (node.modifiers or []):
            continue

        class_name = node.name
        properties = {}
        required_fields = []

        for field_decl in node.fields:
            # A single field declaration can declare multiple variables:
            # e.g.  private String firstName, lastName;
            for declarator in field_decl.declarators:
                field_name = declarator.name
                field_schema = java_type_to_schema(field_decl.type)

                # Apply Bean Validation annotations if present.
                if field_decl.annotations:
                    is_required = apply_validation_annotations(
                        field_decl.annotations, field_schema
                    )
                    if is_required:
                        required_fields.append(field_name)

                properties[field_name] = field_schema

        if not properties:
            # Empty class — not worth emitting a schema for.
            continue

        schema_obj = {
            "type": "object",
            "properties": properties,
        }
        if required_fields:
            schema_obj["required"] = required_fields

        schemas[class_name] = schema_obj

    return schemas


# ---------------------------------------------------------------------------
# Controller parsing  →  paths
# ---------------------------------------------------------------------------

def parse_java_file(java_file_path: str) -> dict:
    """Parse a Spring controller Java source file and return a dict of paths
    suitable for merging into an OpenAPI spec.

    The returned structure mirrors the ``paths`` object of an OpenAPI document.
    When a method parameter is annotated with @RequestBody and its type matches
    a known (non-primitive) class name, the requestBody schema is emitted as a
    $ref rather than an inline ``type: object``.
    """
    with open(java_file_path, 'r', encoding='utf-8') as file:
        java_code = file.read()

    try:
        tree = javalang.parse.parse(java_code)
    except Exception:
        return {}

    paths = {}

    # Spring annotation name → lowercase HTTP verb
    http_methods_map = {
        'GetMapping': 'get',
        'PostMapping': 'post',
        'PutMapping': 'put',
        'DeleteMapping': 'delete',
        'PatchMapping': 'patch',
    }

    for _, node in tree.filter(javalang.tree.TypeDeclaration):
        if not isinstance(node, (javalang.tree.ClassDeclaration,
                                  javalang.tree.InterfaceDeclaration)):
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
                    endpoint_path = (extract_annotation_value(annotation, 'value')
                                     or extract_annotation_value(annotation, 'path')
                                     or '')
                    method_val = extract_annotation_value(annotation, 'method')
                    if method_val:
                        methods = method_val if isinstance(method_val, list) else [method_val]
                        for m in methods:
                            verb = m.lower()
                            if verb.startswith('requestmethod.'):
                                verb = verb.split('.', 1)[1]
                            if verb in http_methods_map.values():
                                http_verb = verb
                                break
                    break

            if not http_verb:
                continue

            def _join(p1, p2):
                """Join two URL path segments without doubling slashes."""
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
                param_type_name = None
                if hasattr(param.type, 'name'):
                    param_type_name = param.type.name
                elif hasattr(param.type, 'pattern_type'):
                    param_type_name = param.type.pattern_type

                # Skip internal Spring / security objects — not real API params.
                if param_type_name in ("Principal", "Authentication",
                                       "HttpServletRequest", "HttpServletResponse",
                                       "Model", "ModelMap", "BindingResult"):
                    continue

                # Build the OpenAPI type schema for this parameter.
                openapi_schema = java_type_to_schema(param.type)

                in_type = 'query'
                required = False

                for pann in param.annotations:
                    if pann.name == 'PathVariable':
                        in_type = 'path'
                        required = True
                        name_override = (extract_annotation_value(pann, 'name')
                                         or extract_annotation_value(pann, 'value'))
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
                        # If the type is a known custom class, reference its
                        # schema in components/schemas instead of inlining.
                        if (param_type_name
                                and param_type_name not in _JAVA_TYPE_MAP
                                and param_type_name not in _COLLECTION_TYPES):
                            body_schema = {
                                "$ref": f"#/components/schemas/{param_type_name}"
                            }
                        else:
                            body_schema = openapi_schema

                        request_body = {
                            "description": f"{param_name} payload",
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": body_schema
                                }
                            }
                        }

                if in_type:
                    parameters.append({
                        "name": param_name,
                        "in": in_type,
                        "required": required,
                        "schema": openapi_schema,
                    })

            # Ensure every {pathVar} in the URL has a corresponding parameter entry.
            path_params = re.findall(r"\{([^/}]+)\}", full_path)
            for pname in path_params:
                found = False
                for p in parameters:
                    if p.get('name') == pname:
                        p['in'] = 'path'
                        p['required'] = True
                        found = True
                        break
                if not found:
                    parameters.append({
                        "name": pname,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    })

            operation_obj = {
                "summary": method.name,
                "operationId": method.name,
                "parameters": parameters,
                "responses": {"200": {"description": "Successful operation"}},
            }
            if request_body:
                operation_obj["requestBody"] = request_body

            paths[full_path][http_verb] = operation_obj

    return paths


# ---------------------------------------------------------------------------
# Backward-compatible single-file wrapper
# ---------------------------------------------------------------------------

def generate_openapi_from_java(java_file_path: str) -> dict:
    """Legacy single-file entry point. Returns an OpenAPI spec dict."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Generated API from Java Source", "version": "1.0.0"},
        "paths": {}
    }
    fragment = parse_java_file(java_file_path)
    spec["paths"].update(fragment)
    return spec


# ---------------------------------------------------------------------------
# Path fragment merging
# ---------------------------------------------------------------------------

def merge_paths(spec_paths: dict, fragment: dict):
    """Merge *fragment* path entries into *spec_paths* in-place.

    If the same URL path already exists, HTTP method operations are merged;
    last writer wins on true conflicts (same path + same verb).
    """
    for path, ops in fragment.items():
        if path not in spec_paths:
            spec_paths[path] = ops
        else:
            spec_paths[path].update(ops)


def merge_schemas(spec_schemas: dict, fragment: dict):
    """Merge *fragment* schema entries into *spec_schemas* in-place.

    Last writer wins on name collisions (same as path merging behaviour).
    """
    spec_schemas.update(fragment)


def clean_broken_schema_refs(spec: dict):
    """Post-process the spec to remove $ref entries pointing to non-existent schemas.

    This ensures the generated spec is valid even if some referenced types weren't
    discovered or parsed. Any broken $ref is replaced with {"type": "object"}.
    Fixes refs in both components/schemas and operation parameters.
    """
    if "components" not in spec or "schemas" not in spec["components"]:
        defined_schemas = set()
    else:
        defined_schemas = set(spec["components"]["schemas"].keys())
    
    def fix_schema(obj):
        """Recursively walk through a schema and fix broken refs."""
        if isinstance(obj, dict):
            # If this object is a $ref to a non-existent schema, replace it
            if "$ref" in obj and len(obj) == 1:
                ref_name = obj["$ref"].split("/")[-1]
                if ref_name not in defined_schemas:
                    # Replace with basic object type
                    return {"type": "object"}
            else:
                # Recursively fix nested objects and arrays
                for key, val in obj.items():
                    obj[key] = fix_schema(val)
        elif isinstance(obj, list):
            return [fix_schema(item) for item in obj]
        return obj
    
    # Fix all schemas in the components section
    if "components" in spec and "schemas" in spec["components"]:
        for schema_name, schema_def in spec["components"]["schemas"].items():
            spec["components"]["schemas"][schema_name] = fix_schema(schema_def)
    
    # Also fix any broken refs in operation parameters (paths section)
    if "paths" in spec:
        for path_def in spec["paths"].values():
            for operation in path_def.values():
                if isinstance(operation, dict):
                    if "parameters" in operation:
                        for param in operation["parameters"]:
                            if "schema" in param:
                                param["schema"] = fix_schema(param["schema"])
                    if "requestBody" in operation:
                        rb = operation["requestBody"]
                        if "content" in rb:
                            for content_type, content in rb["content"].items():
                                if "schema" in content:
                                    content["schema"] = fix_schema(content["schema"])


# ---------------------------------------------------------------------------
# Full project generation
# ---------------------------------------------------------------------------

def generate_openapi_from_project(project_root: str,
                                   include_packages=None,
                                   exclude_packages=None) -> tuple:
    """Scan an entire Spring Boot project and return a combined OpenAPI spec.

    Returns a tuple of (spec_dict, controller_files_list, model_files_list).

    The spec includes:
      - ``paths``          built from Spring controller annotations
      - ``components/schemas``  built from DTO/model Bean Validation annotations
    """
    if include_packages:
        include_packages = [p.strip() for p in include_packages if p.strip()]
    if exclude_packages:
        exclude_packages = [p.strip() for p in exclude_packages if p.strip()]

    src = find_source_directory(project_root)

    # Discover controller files (have Spring mapping annotations).
    controller_files = discover_java_files(src, include_packages, exclude_packages)

    # Discover model/DTO files (have validation annotations, no Spring mappings).
    model_files = discover_model_files(src, include_packages, exclude_packages)

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Generated API from Java Source", "version": "1.0.0"},
        "paths": {},
        "components": {
            "schemas": {}
        }
    }

    # Parse every controller file and merge its paths.
    for jf in controller_files:
        fragment = parse_java_file(jf)
        merge_paths(spec["paths"], fragment)

    # Parse every model/DTO file and merge its schemas.
    for mf in model_files:
        schema_fragment = parse_java_model_file(mf)
        merge_schemas(spec["components"]["schemas"], schema_fragment)

    # Also scan controller files for any inline model classes they might declare.
    for jf in controller_files:
        schema_fragment = parse_java_model_file(jf)
        merge_schemas(spec["components"]["schemas"], schema_fragment)

    # Clean up any broken schema references (refs to undiscovered types).
    clean_broken_schema_refs(spec)

    # Remove the components block entirely if no schemas were found,
    # to keep the output clean for projects with no annotated models.
    if not spec["components"]["schemas"]:
        del spec["components"]

    return spec, controller_files, model_files


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

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
    parser.add_argument('--output-dir',
                        help='Directory in which to write the OpenAPI spec')

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
    spec, controller_files, model_files = generate_openapi_from_project(
        root,
        include_packages=(
            args.include_packages.split(',') if args.include_packages else None
        ),
        exclude_packages=(
            args.exclude_packages.split(',') if args.exclude_packages else None
        ),
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
        print(f"Processed {len(controller_files)} controller file(s), "
              f"{len(model_files)} model/DTO file(s)")
    except Exception as e:
        print(f"Failed to write spec: {e}")