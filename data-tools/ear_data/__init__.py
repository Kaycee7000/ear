"""Remote data tools. The worker can import ear_data.plan independently."""

__all__ = ["get_data_result", "prepare_dataset_upload", "run_data_job"]


def __getattr__(name):
    if name in __all__:
        from . import agent

        return getattr(agent, name)
    raise AttributeError(name)
