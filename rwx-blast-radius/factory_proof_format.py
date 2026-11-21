"""Shared baseline proof identity; AST hashes are interpreter-version specific."""
import ast
import hashlib
import sys

REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
SCHEMA = 1
PROOF_KIND = 'syntactic-closure-construction-only'


def python_version():
    return [sys.version_info.major, sys.version_info.minor]


def ast_hash(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
