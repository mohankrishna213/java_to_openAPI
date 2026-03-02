"""Convert Spring Boot controller interfaces into OpenAPI specs.

This script can operate in three modes:

* **Single-file mode** (legacy): provide a path to a Java source file.
  Example: ``python java_to_openapi.py MyController.java``

* **Project mode**: point at a Spring Boot project root containing a
  ``pom.xml``. The tool will scan ``src/main/java`` (and other standard
  locations) for Java files that contain Spring Web annotations and merge all
  discovered endpoints into one OpenAPI 3.0 document. You can also filter
  packages with ``--include-packages``/``--exclude-packages``.

* **Remote mode** (GitHub or Bitbucket): pass a repository URL with ``--repo``.
  The repo is shallow-cloned into a temporary directory using GitPython,
  converted, the YAML is written to disk, and the temp directory is deleted
  automatically. Supports public and private repos on both providers.

  Authentication
  --------------
  GitHub         : ``--token ghp_xxx``  or  env var ``GITHUB_TOKEN``
  Bitbucket (app password)  : ``--token ATBBxxx --bb-user myusername``
                              or env vars ``BITBUCKET_TOKEN`` / ``BITBUCKET_USER``
  Bitbucket (repo access token): ``--token TOKEN``  (no username needed)

  Branch selection
  ----------------
  Pass ``--branch develop`` to check out a specific branch or tag.
  A branch embedded in the URL (e.g. ``.../tree/develop`` for GitHub or
  ``.../src/develop`` for Bitbucket) is also detected automatically.

  Debugging
  ---------
  Pass ``--keep-temp`` to suppress cleanup of the cloned directory.

  Examples::

      python java_to_openapi.py --repo https://github.com/acme/my-service
      python java_to_openapi.py --repo https://bitbucket.org/acme/my-service
      python java_to_openapi.py --repo https://github.com/acme/private --token ghp_xxx
      python java_to_openapi.py --repo https://bitbucket.org/acme/private \\
          --token ATBBxxx --bb-user myusername --branch develop

The script also scans for DTO/model classes and emits a
``components/schemas`` block in the generated spec. It reads Bean Validation
annotations (@NotNull, @NotBlank, @Size, @Min, @Max, @Pattern, @Email,
@Positive, @Negative, @Digits, @DecimalMin, @DecimalMax) and maps them to
OpenAPI 3.0 JSON Schema constraint keywords.  Enum types declared with
``enum`` keywords are converted into string schemas with an ``enum`` array.

When run with no arguments the script interactively asks for the project path.
The generated YAML is written to ``<project_root>/openapi-spec/openapi.yaml``
by default.

Dependencies
------------
  pip install javalang pyyaml gitpython
"""

import argparse
import os
import re
import shutil
import sys
import tempfile

import git                  # GitPython  →  pip install gitpython
import git.exc              # Typed Git exceptions
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
            # Must either have validation annotations or declare an enum.
            if not has_validation_annotations(code) and 'enum ' not in code:
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
# Remote repository support  (GitHub + Bitbucket via GitPython)
# ---------------------------------------------------------------------------

# --- URL patterns for GitHub ---
# Matches: https://github.com/owner/repo  and  https://github.com/owner/repo/tree/branch
_GITHUB_HTTPS_RE = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)"
    r"(?:\.git)?(?:/tree/(?P<branch>[^/?#]+))?"
)
# Matches: git@github.com:owner/repo.git
_GITHUB_SSH_RE = re.compile(
    r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?"
)

# --- URL patterns for Bitbucket ---
# Note: Bitbucket uses /src/<branch> rather than GitHub's /tree/<branch>
_BITBUCKET_HTTPS_RE = re.compile(
    r"https?://(?:www\.)?bitbucket\.org/(?P<owner>[^/]+)/(?P<repo>[^/.]+)"
    r"(?:\.git)?(?:/src/(?P<branch>[^/?#]+))?"
)
# Matches: git@bitbucket.org:owner/repo.git
_BITBUCKET_SSH_RE = re.compile(
    r"git@bitbucket\.org:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?"
)


def parse_repo_url(url: str) -> dict:
    """Parse a GitHub or Bitbucket URL and return a normalised dict.

    The returned dict always has these keys:
      provider  : 'github' or 'bitbucket'
      owner     : repository owner / workspace name
      repo      : repository name (without .git)
      branch    : branch embedded in the URL, or None
      clone_url : clean HTTPS clone URL (no credentials yet)

    Why return a clean clone_url without credentials?
    Because we want to build the credential-injected URL separately, in one
    place, so the token never accidentally ends up in a log or error message
    unless we explicitly choose to redact it first.
    """
    url = url.strip()

    m = _GITHUB_HTTPS_RE.match(url)
    if m:
        return {
            "provider":  "github",
            "owner":     m.group("owner"),
            "repo":      m.group("repo"),
            "branch":    m.group("branch"),   # None if not in URL
            "clone_url": f"https://github.com/{m.group('owner')}/{m.group('repo')}.git",
        }

    m = _GITHUB_SSH_RE.match(url)
    if m:
        return {
            "provider":  "github",
            "owner":     m.group("owner"),
            "repo":      m.group("repo"),
            "branch":    None,
            # SSH URLs stay as-is; credential injection only applies to HTTPS
            "clone_url": url if url.endswith(".git") else url + ".git",
        }

    m = _BITBUCKET_HTTPS_RE.match(url)
    if m:
        return {
            "provider":  "bitbucket",
            "owner":     m.group("owner"),
            "repo":      m.group("repo"),
            "branch":    m.group("branch"),
            "clone_url": (
                f"https://bitbucket.org/{m.group('owner')}/{m.group('repo')}.git"
            ),
        }

    m = _BITBUCKET_SSH_RE.match(url)
    if m:
        return {
            "provider":  "bitbucket",
            "owner":     m.group("owner"),
            "repo":      m.group("repo"),
            "branch":    None,
            "clone_url": url if url.endswith(".git") else url + ".git",
        }

    raise ValueError(
        f"Could not recognise '{url}' as a GitHub or Bitbucket URL.\n"
        "Supported URL forms:\n"
        "  https://github.com/owner/repo\n"
        "  https://github.com/owner/repo/tree/branch\n"
        "  git@github.com:owner/repo.git\n"
        "  https://bitbucket.org/owner/repo\n"
        "  https://bitbucket.org/owner/repo/src/branch\n"
        "  git@bitbucket.org:owner/repo.git"
    )


def _build_auth_url(clone_url: str, provider: str,
                    token: str, bb_user: str = None) -> str:
    """Embed authentication credentials into an HTTPS clone URL.

    GitHub:
      The token alone is placed as the username component:
        https://ghp_xxx@github.com/owner/repo.git
      Git treats a bare token as both username and password,
      which is what GitHub's HTTPS authentication expects.

    Bitbucket (App Password):
      Bitbucket requires an explicit username alongside the app password:
        https://myusername:ATBBxxx@bitbucket.org/owner/repo.git
      If no username is supplied we fall back to 'x-token-auth', which is
      Bitbucket's official placeholder when using repository access tokens
      (not app passwords).

    SSH URLs are returned unchanged because they don't carry credentials in
    the URL — they authenticate via your local SSH key instead.
    """
    if not token or not clone_url.startswith("https://"):
        return clone_url   # SSH or no token → nothing to inject

    if provider == "bitbucket":
        # For Bitbucket app passwords a real username is required.
        # For repository access tokens 'x-token-auth' is the documented placeholder.
        user = bb_user or "x-token-auth"
        credential = f"{user}:{token}"
    else:
        # GitHub: bare token is sufficient
        credential = token

    return clone_url.replace("https://", f"https://{credential}@", 1)


def _redact_url(url: str) -> str:
    """Replace any embedded credential in a URL with *** for safe printing.

    Turns  https://ghp_secrettoken@github.com/...
    into   https://***@github.com/...

    This is used whenever we need to print a clone URL to the terminal
    so that tokens never appear in plain text output or stack traces.
    """
    return re.sub(r"(https://)([^@]+)(@)", r"\1***\3", url)


# ---------------------------------------------------------------------------
# GitPython progress reporter
# ---------------------------------------------------------------------------

class _CloneProgress(git.RemoteProgress):
    """Real-time clone progress printed to stdout.

    GitPython calls update() each time it receives a progress line from git.
    We override it to print a human-readable status line and overwrite the
    same terminal line using a carriage return (\r) so the output doesn't
    scroll — it stays in one place and updates in real time.

    The different 'op_code' values GitPython sends correspond to stages like
    counting objects, compressing, receiving data, and resolving deltas.
    """

    # Map GitPython's numeric stage codes to short human-readable labels.
    # These are bit-flags defined in git.RemoteProgress.
    _STAGE_LABELS = {
        git.RemoteProgress.COUNTING:    "Counting objects",
        git.RemoteProgress.COMPRESSING: "Compressing",
        git.RemoteProgress.WRITING:     "Writing",
        git.RemoteProgress.RECEIVING:   "Receiving",
        git.RemoteProgress.RESOLVING:   "Resolving deltas",
        git.RemoteProgress.FINDING_SOURCES: "Finding sources",
        git.RemoteProgress.CHECKING_OUT:    "Checking out files",
    }

    def update(self, op_code, cur_count, max_count=None, message=""):
        # op_code is a bitmask; mask out the BEGIN/END flags to get the stage.
        stage = op_code & self.OP_MASK

        label = self._STAGE_LABELS.get(stage, "Working")
        if max_count and max_count > 0:
            pct = int(100 * cur_count / max_count)
            line = f"\r[clone] {label}: {cur_count}/{int(max_count)} ({pct}%)"
        else:
            line = f"\r[clone] {label}: {cur_count}"

        # \r moves the cursor to the start of the current line so the next
        # write overwrites it rather than creating a new line.
        print(line, end="", flush=True)

    def finalize(self):
        # Print a newline after the last progress update so subsequent output
        # starts on a fresh line rather than overwriting the progress text.
        print()


# ---------------------------------------------------------------------------
# Core clone + cleanup functions
# ---------------------------------------------------------------------------

def clone_repo(repo_url: str,
               token: str = None,
               branch: str = None,
               bb_user: str = None,
               show_progress: bool = True) -> str:
    """Clone a GitHub or Bitbucket repository into a new temporary directory.

    Parameters
    ----------
    repo_url : str
        Any URL form recognised by parse_repo_url().
    token : str, optional
        Authentication token. For GitHub this is a Personal Access Token
        (PAT). For Bitbucket this is either an App Password or a Repository
        Access Token. If not provided, the function checks the environment
        variables GITHUB_TOKEN (GitHub) and BITBUCKET_TOKEN (Bitbucket).
    branch : str, optional
        Branch or tag name to check out. When None the remote's default
        branch is used. Overrides any branch embedded in the URL.
    bb_user : str, optional
        Bitbucket username, required when using Bitbucket App Passwords.
        Falls back to the BITBUCKET_USER environment variable.
        Not needed for Bitbucket Repository Access Tokens or GitHub.
    show_progress : bool
        Whether to print a live progress line during the clone.

    Returns
    -------
    str
        Absolute path to the temporary directory containing the clone.
        The caller must delete this directory when finished by calling
        cleanup_temp_repo() or shutil.rmtree().

    Raises
    ------
    ValueError
        If the URL cannot be parsed as a GitHub or Bitbucket URL.
    RuntimeError
        If GitPython cannot find git on the system PATH, or if the clone
        fails (wrong URL, bad token, network error, etc.).
    """
    parsed = parse_repo_url(repo_url)   # raises ValueError for bad URLs

    # Resolve effective branch: CLI arg beats URL-embedded beat default
    effective_branch = branch or parsed.get("branch")

    # Resolve credentials from arguments, then fall back to env vars
    if parsed["provider"] == "github":
        effective_token = token or os.environ.get("GITHUB_TOKEN", "")
    else:
        effective_token = token or os.environ.get("BITBUCKET_TOKEN", "")

    effective_bb_user = bb_user or os.environ.get("BITBUCKET_USER", "")

    # Build the authenticated clone URL (token embedded for HTTPS)
    auth_url = _build_auth_url(
        parsed["clone_url"],
        provider=parsed["provider"],
        token=effective_token,
        bb_user=effective_bb_user or None,
    )

    # GitPython requires git to be installed just like subprocess would.
    # We check for it explicitly so we can raise a clear error message
    # rather than letting GitPython raise a cryptic internal error.
    try:
        git.Git().version()   # runs `git --version` under the hood
    except git.exc.GitCommandNotFound:
        raise RuntimeError(
            "git was not found on your PATH. "
            "Please install Git (https://git-scm.com) and try again."
        )

    # Create an isolated temp directory. mkdtemp() sets permissions so only
    # the current user can read it — important since we store tokens in URLs.
    temp_dir = tempfile.mkdtemp(prefix=f"java_openapi_{parsed['repo']}_")

    # Log the redacted URL so the user can see what's happening without
    # ever seeing their token in plain text.
    safe_url = _redact_url(auth_url)
    branch_info = f" (branch: {effective_branch})" if effective_branch else ""
    print(f"[repo] Cloning {safe_url}{branch_info} …")

    try:
        # Repo.clone_from() is GitPython's equivalent of `git clone`.
        #
        # depth=1  →  shallow clone (only the latest commit snapshot).
        #             This is critical for speed — a repo with years of
        #             history can be hundreds of MB but we only need the
        #             current file contents, so depth=1 is always correct here.
        #
        # branch   →  if None, GitPython omits the flag and git uses the
        #             remote's default branch (usually main or master).
        #
        # progress →  our custom _CloneProgress reporter (optional).

        clone_kwargs = {
            "depth": 1,
            "progress": _CloneProgress() if show_progress else None,
        }
        if effective_branch:
            clone_kwargs["branch"] = effective_branch

        git.Repo.clone_from(auth_url, temp_dir, **clone_kwargs)

    except git.exc.GitCommandError as exc:
        # GitCommandError carries the full stderr from git in exc.stderr.
        # We redact the URL from it before showing it to the user so that
        # error messages never reveal the embedded token.
        shutil.rmtree(temp_dir, ignore_errors=True)   # clean up empty dir
        safe_stderr = _redact_url(str(exc.stderr or ""))
        raise RuntimeError(
            f"Clone failed for {safe_url}{branch_info}.\n"
            f"Git said: {safe_stderr.strip()}"
        ) from None   # 'from None' suppresses the original traceback chain
                      # so the user sees our clean message, not GitPython internals

    print(
        f"[repo] Cloned '{parsed['owner']}/{parsed['repo']}' "
        f"({parsed['provider']}) → {temp_dir}"
    )
    return temp_dir


def cleanup_temp_repo(temp_dir: str, keep: bool = False):
    """Delete the temporary clone directory.

    When *keep* is True the directory is preserved and its path is printed.
    This is useful for manually inspecting the cloned files if conversion
    produces unexpected results.
    """
    if keep:
        print(f"[repo] --keep-temp: clone preserved at {temp_dir}")
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("[repo] Temporary clone removed.")


# ---------------------------------------------------------------------------
# High-level remote → OpenAPI orchestrator
# ---------------------------------------------------------------------------

def generate_openapi_from_remote(
    repo_url: str,
    output_dir: str = None,
    token: str = None,
    branch: str = None,
    bb_user: str = None,
    keep_temp: bool = False,
    include_packages=None,
    exclude_packages=None,
) -> tuple:
    """Clone a remote repo and convert it to an OpenAPI spec.

    This is the single function that ties together URL parsing, cloning,
    Maven metadata extraction, Java analysis, and YAML output.  It is
    designed so that callers (including the CLI __main__ block) only need
    to call one function for the entire remote-repo workflow.

    The try/finally structure is intentional and important.  Python's
    finally block runs regardless of whether the try block completed
    normally or raised an exception — even a KeyboardInterrupt.  This
    guarantees that the temporary clone directory is always deleted
    (unless --keep-temp was requested), so we never leave gigabytes of
    cloned code in /tmp if something goes wrong mid-conversion.

    Returns
    -------
    tuple : (spec_dict, output_yaml_path, temp_dir_path)
    """
    temp_dir = None
    try:
        temp_dir = clone_repo(
            repo_url,
            token=token,
            branch=branch,
            bb_user=bb_user,
        )

        pom = os.path.join(temp_dir, "pom.xml")
        if not os.path.exists(pom):
            raise RuntimeError(
                f"No pom.xml found in the cloned repository root.\n"
                "This tool supports Maven projects. "
                "Make sure you're pointing at the project root, not a subdirectory."
            )

        meta = extract_maven_metadata(pom)

        spec, controller_files, model_files = generate_openapi_from_project(
            temp_dir,
            include_packages=include_packages,
            exclude_packages=exclude_packages,
        )

        spec["info"]["title"]   = meta.get("title",   spec["info"]["title"])
        spec["info"]["version"] = meta.get("version", spec["info"]["version"])

        # Record the source repo URL in the spec as an OpenAPI extension field.
        # The 'x-' prefix is the standard OpenAPI convention for custom metadata
        # fields — tools that don't understand it simply ignore it.
        parsed = parse_repo_url(repo_url)
        if parsed["provider"] == "github":
            spec["info"]["x-source-repo"] = (
                f"https://github.com/{parsed['owner']}/{parsed['repo']}"
            )
        else:
            spec["info"]["x-source-repo"] = (
                f"https://bitbucket.org/{parsed['owner']}/{parsed['repo']}"
            )

        yaml_output = yaml.dump(spec, sort_keys=False, default_flow_style=False)

        # Write output to disk.  We cannot write inside temp_dir because
        # it will be deleted by the finally block, so we use a separate
        # output directory — defaulting to ./<repo-name>-openapi-spec/
        # in the current working directory.
        out_folder = output_dir or os.path.join(
            os.getcwd(), f"{parsed['repo']}-openapi-spec"
        )
        os.makedirs(out_folder, exist_ok=True)
        out_path = os.path.join(out_folder, "openapi.yaml")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(yaml_output)

        print(f"[repo] OpenAPI spec written to {out_path}")
        print(
            f"[repo] Processed {len(controller_files)} controller file(s), "
            f"{len(model_files)} model/DTO file(s)"
        )

        return spec, out_path, temp_dir

    finally:
        # Always runs — success, error, or Ctrl-C
        if temp_dir is not None:
            cleanup_temp_repo(temp_dir, keep=keep_temp)


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
        # support enum declarations by emitting a simple string-with-enum schema
        if isinstance(node, javalang.tree.EnumDeclaration):
            enum_name = node.name
            # javalang stores constants under node.body.constants
            consts = []
            if getattr(node, 'body', None):
                consts = getattr(node.body, 'constants', []) or []
            values = [c.name for c in consts]
            if values:
                schemas[enum_name] = {"type": "string", "enum": values}
            continue

        # Only process concrete classes (interfaces skipped)
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
        description=(
            "Convert a Spring Boot project's controllers into an OpenAPI YAML spec.\n\n"
            "Works with local project directories, GitHub repos, and Bitbucket repos."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Local Maven project
  python java_to_openapi.py /path/to/my-spring-app

  # Public GitHub repo
  python java_to_openapi.py --repo https://github.com/acme/my-service

  # Public Bitbucket repo
  python java_to_openapi.py --repo https://bitbucket.org/acme/my-service

  # Private GitHub repo (token via flag or env var)
  python java_to_openapi.py --repo https://github.com/acme/private --token ghp_xxx
  GITHUB_TOKEN=ghp_xxx python java_to_openapi.py --repo https://github.com/acme/private

  # Private Bitbucket repo (app password - needs username too)
  python java_to_openapi.py --repo https://bitbucket.org/acme/private \\
      --token ATBBxxx --bb-user myusername

  # Private Bitbucket repo (repository access token - no username needed)
  python java_to_openapi.py --repo https://bitbucket.org/acme/private --token TOKEN

  # Specific branch, custom output directory, package filter
  python java_to_openapi.py --repo https://github.com/acme/my-service \\
      --branch develop --output-dir ./specs --include-packages com.acme.api

  # Keep the temp clone for debugging
  python java_to_openapi.py --repo https://github.com/acme/my-service --keep-temp
        """,
    )

    # project_root (positional) and --repo are mutually exclusive:
    # you either point at a local directory OR a remote URL, never both.
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "project_root",
        nargs="?",
        help="Root directory of a local Spring Boot Maven project (must contain pom.xml).",
    )
    source_group.add_argument(
        "--repo",
        metavar="URL",
        help=(
            "GitHub or Bitbucket repository URL to clone and convert. "
            "Accepts HTTPS, HTTPS-with-branch, and SSH forms."
        ),
    )

    # Remote-only options, grouped together in --help output
    remote_group = parser.add_argument_group("Remote repository options")
    remote_group.add_argument(
        "--token",
        metavar="TOKEN",
        help=(
            "Authentication token for private repositories. "
            "For GitHub: a Personal Access Token (PAT). "
            "For Bitbucket: an App Password or a Repository Access Token. "
            "Falls back to GITHUB_TOKEN or BITBUCKET_TOKEN env vars."
        ),
    )
    remote_group.add_argument(
        "--bb-user",
        metavar="USERNAME",
        help=(
            "Bitbucket username, required when using App Passwords. "
            "Not needed for Repository Access Tokens. "
            "Falls back to the BITBUCKET_USER env var."
        ),
    )
    remote_group.add_argument(
        "--branch",
        metavar="BRANCH",
        help=(
            "Branch or tag to check out. Overrides any branch in the URL. "
            "Defaults to the repository's default branch."
        ),
    )
    remote_group.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete the temporary clone directory after conversion (useful for debugging).",
    )

    # Options shared by both local and remote modes
    parser.add_argument(
        "--include-packages",
        help="Comma-separated package prefixes to include (e.g. com.acme.api,com.acme.dto).",
    )
    parser.add_argument(
        "--exclude-packages",
        help="Comma-separated package prefixes to exclude.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Where to write openapi.yaml. "
            "Local default: <project_root>/openapi-spec/. "
            "Remote default: ./<repo-name>-openapi-spec/ in current directory."
        ),
    )

    args = parser.parse_args()

    include_pkgs = args.include_packages.split(",") if args.include_packages else None
    exclude_pkgs = args.exclude_packages.split(",") if args.exclude_packages else None

    # ------------------------------------------------------------------
    # Remote mode  (--repo flag provided)
    # ------------------------------------------------------------------
    if args.repo:
        try:
            generate_openapi_from_remote(
                repo_url=args.repo,
                output_dir=args.output_dir,
                token=args.token,
                branch=args.branch,
                bb_user=args.bb_user,
                keep_temp=args.keep_temp,
                include_packages=include_pkgs,
                exclude_packages=exclude_pkgs,
            )
        except (ValueError, RuntimeError) as exc:
            # ValueError  → bad URL format from parse_repo_url()
            # RuntimeError → git not found, clone failed, no pom.xml
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Local project mode  (original behaviour, fully preserved)
    # ------------------------------------------------------------------
    root = args.project_root
    if not root:
        root = input("Enter Spring Boot project root path: ").strip()
    root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)

    pom = os.path.join(root, "pom.xml")
    if not os.path.exists(pom):
        print(
            f"Error: No pom.xml found in {root}.\n"
            "Please point at the root of a Maven project.",
            file=sys.stderr,
        )
        sys.exit(1)

    meta = extract_maven_metadata(pom)
    spec, controller_files, model_files = generate_openapi_from_project(
        root,
        include_packages=include_pkgs,
        exclude_packages=exclude_pkgs,
    )
    spec["info"]["title"]   = meta.get("title",   spec["info"]["title"])
    spec["info"]["version"] = meta.get("version", spec["info"]["version"])

    yaml_output = yaml.dump(spec, sort_keys=False, default_flow_style=False)
    print(yaml_output)

    out_folder = args.output_dir or os.path.join(root, "openapi-spec")
    os.makedirs(out_folder, exist_ok=True)
    out_path = os.path.join(out_folder, "openapi.yaml")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(yaml_output)
        print(f"OpenAPI spec written to {out_path}")
        print(
            f"Processed {len(controller_files)} controller file(s), "
            f"{len(model_files)} model/DTO file(s)"
        )
    except Exception as exc:
        print(f"Failed to write spec: {exc}", file=sys.stderr)