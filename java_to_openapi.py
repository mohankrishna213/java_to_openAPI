import javalang
import yaml
import sys
import os

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

def generate_openapi_from_java(java_file_path):
    with open(java_file_path, 'r') as file:
        java_code = file.read()

    tree = javalang.parse.parse(java_code)
    
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Generated API from Java Source",
            "version": "1.0.0"
        },
        "paths": {}
    }

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
        # only interested in interface or class declarations
        if not isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
            continue
        # skip non-controller classes (naive heuristic: has any mapping on class or methods)
        has_mapping = False
        base_path = ""

        # class-level annotations
        for annotation in node.annotations:
            if annotation.name == 'RequestMapping':
                has_mapping = True
                # either default literal, or named value/path
                base_path = (extract_annotation_value(annotation)
                             or extract_annotation_value(annotation, 'value')
                             or extract_annotation_value(annotation, 'path')
                             or '')
            elif annotation.name in http_methods_map:
                # rare, but could have @GetMapping on class
                has_mapping = True
                base_path = extract_annotation_value(annotation) or ''

        # if the node has no mapping, we still want to scan methods; we won't skip

        # iterate methods
        for method in node.methods:
            http_verb = None
            endpoint_path = ''
            # detect method-level mapping
            for annotation in method.annotations:
                if annotation.name in http_methods_map:
                    http_verb = http_methods_map[annotation.name]
                    endpoint_path = extract_annotation_value(annotation) or ''
                    has_mapping = True
                    break
                if annotation.name == 'RequestMapping':
                    has_mapping = True
                    endpoint_path = extract_annotation_value(annotation, 'value') or extract_annotation_value(annotation, 'path') or ''
                    # figure out method attribute(s)
                    method_val = extract_annotation_value(annotation, 'method')
                    if method_val:
                        # could be a list or single
                        methods = method_val if isinstance(method_val, list) else [method_val]
                        # strip enum prefix if exists
                        for m in methods:
                            verb = m.lower()
                            if verb.startswith('requestmethod.'):
                                verb = verb.split('.',1)[1]
                            if verb in http_methods_map.values():
                                http_verb = verb
                                break
                    # if no method found, leave None and skip later
                    break
            if not http_verb:
                continue

            # construct full path, avoiding double slashes
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

            if full_path not in openapi_spec["paths"]:
                openapi_spec["paths"][full_path] = {}

            # extract parameters and request body
            parameters = []
            request_body = None
            for param in method.parameters:
                param_name = param.name
                param_type = None
                if hasattr(param.type, 'name'):
                    param_type = param.type.name
                elif hasattr(param.type, 'pattern_type'):
                    param_type = param.type.pattern_type

                # skip common security/context parameters
                if param_type in ["Principal", "Authentication", "HttpServletRequest"]:
                    continue

                # default mapping
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
                    # non-primitive/custom types represented as object
                    openapi_type = "object"

                # inspect annotations on parameter
                in_type = 'query'
                required = False
                for pann in param.annotations:
                    if pann.name == 'PathVariable':
                        in_type = 'path'
                        required = True
                        # override name if provided
                        name_override = extract_annotation_value(pann, 'name') or extract_annotation_value(pann, 'value')
                        if name_override:
                            param_name = name_override
                    elif pann.name == 'RequestParam':
                        in_type = 'query'
                        # by default Spring request params are required unless set false or a defaultValue is provided
                        required = True
                        req_val = extract_annotation_value(pann, 'required')
                        if isinstance(req_val, str) and req_val.lower() == 'false':
                            required = False
                        # defaultValue indicates the param will be substituted if not supplied
                        if extract_annotation_value(pann, 'defaultValue') is not None:
                            required = False
                    elif pann.name == 'RequestHeader':
                        in_type = 'header'
                    elif pann.name == 'RequestBody':
                        # defer to requestBody section
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

            operation_obj = {
                "summary": method.name,
                "operationId": method.name,
                "parameters": parameters,
                "responses": {"200": {"description": "Successful operation"}}
            }
            if request_body:
                operation_obj["requestBody"] = request_body

            openapi_spec["paths"][full_path][http_verb] = operation_obj

    return openapi_spec

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python java_to_openapi.py <path_to_java_file> [output_folder]")
        sys.exit(1)

    java_file = sys.argv[1]
    out_folder = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.exists(java_file):
        print(f"File not found: {java_file}")
        sys.exit(1)

    spec = generate_openapi_from_java(java_file)
    
    # Output as YAML
    yaml_output = yaml.dump(spec, sort_keys=False, default_flow_style=False)
    print(yaml_output)

    # write to resources folder or specified output directory
    if not out_folder:
        out_folder = os.path.join(os.path.dirname(java_file), 'resources')
    if not os.path.exists(out_folder):
        os.makedirs(out_folder, exist_ok=True)

    base = os.path.splitext(os.path.basename(java_file))[0]
    out_path = os.path.join(out_folder, f"{base}.yaml")
    try:
        with open(out_path, 'w') as f:
            f.write(yaml_output)
        print(f"OpenAPI spec written to {out_path}")
    except Exception as e:
        print(f"Failed to write spec: {e}")