"""Remote code checks. The worker image imports ear_code.plan on its own."""

__all__ = ["get_code_result", "prepare_source_upload", "run_code_check"]


def __getattr__(name):
    if name in __all__:
        from . import agent

        return getattr(agent, name)
    raise AttributeError(name)
