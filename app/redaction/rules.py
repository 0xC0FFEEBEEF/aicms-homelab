from __future__ import annotations

from pathlib import PurePosixPath


DEFAULT_IGNORE_PATTERNS = [
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".idea",
    ".vscode",
    "node_modules",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
    "tmp",
    "temp",
]

SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "known_hosts",
    "authorized_keys",
    "wallet.dat",
}

SENSITIVE_SUFFIXES = {".pem", ".key", ".crt", ".p12", ".sqlite", ".sqlite3", ".db"}
SENSITIVE_PATH_PARTS = {"browser", "wallet", ".ssh", ".gnupg"}
SAFE_TEXT_FILE_NAMES = {
    "Dockerfile",
    "README",
    "README.md",
    "README.txt",
    "Makefile",
    "Procfile",
    ".gitignore",
    ".dockerignore",
    ".env.example",
}
SAFE_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".env.example",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".tf",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def should_ignore_path(path: str, ignore_patterns: list[str]) -> bool:
    pure_path = PurePosixPath(path)
    names = set(pure_path.parts)

    if names.intersection(SENSITIVE_PATH_PARTS):
        return True

    if pure_path.name in SENSITIVE_FILE_NAMES:
        return True

    if any(pure_path.name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return True

    for pattern in ignore_patterns:
        if pattern in names or pure_path.name == pattern:
            return True

    return False


def filter_tree_entries(entries: list[dict[str, str]], ignore_patterns: list[str]) -> list[dict[str, str]]:
    return [entry for entry in entries if not should_ignore_path(entry["path"], ignore_patterns)]


def is_safe_text_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    if should_ignore_path(path, []):
        return False
    if pure_path.name in SAFE_TEXT_FILE_NAMES:
        return True
    return any(pure_path.name.endswith(suffix) for suffix in SAFE_TEXT_SUFFIXES)
