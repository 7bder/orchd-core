import importlib.metadata


def _get_version() -> str:
    try:
        return importlib.metadata.version("orchd")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0.dev0"


__version__ = _get_version()

from orchd.gitops import get_current_branch, get_default_branch

__all__ = ["__version__", "get_current_branch", "get_default_branch"]
