from .settings import config as _real_config


class _ConfigProxy:
    """Lightweight proxy around the shared config instance."""

    def __getattr__(self, name):  # type: ignore[override]
        return getattr(_real_config, name)

    def __setattr__(self, name, value):  # type: ignore[override]
        setattr(_real_config, name, value)


config = _ConfigProxy()

__all__ = ["config"]
