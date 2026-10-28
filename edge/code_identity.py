"""Code identity of the frozen decision and feature modules, independent of formatting.

`ast_sha256` is the SHA-256 of the module's syntax tree with docstrings removed (ast.dump without positions), so
reformatting and docstring edits keep it while any change to the executed code changes it. edge/code_provenance.json
pairs it with the byte hashes recorded when the models were trained and evaluated.
"""

import ast
import hashlib
from pathlib import Path


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return tree


def ast_sha256(path, replace=None):
    """Hash of the code of `path`; `replace` maps string constants to substitute before hashing."""
    tree = _strip_docstrings(ast.parse(Path(path).read_text(encoding='utf-8')))
    for node in ast.walk(tree):
        if replace and isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in replace:
            node.value = replace[node.value]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
