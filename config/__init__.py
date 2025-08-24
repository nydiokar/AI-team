from .settings import config as _real_config


class _ConfigProxy:
    """Lightweight proxy that reloads env-driven fields on each access.

    This makes tests like setting os.environ then re-importing `config`
    observe updated values without requiring a hard module reload.
    """

    def __getattr__(self, name):  # type: ignore[override]
        try:
            _real_config.reload_from_env()
        except Exception:
            pass
        return getattr(_real_config, name)


config = _ConfigProxy()

__all__ = ["config"]