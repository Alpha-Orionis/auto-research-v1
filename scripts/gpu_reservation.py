"""One reservation namespace for foreground experiments and all task types."""
from contextlib import ExitStack, contextmanager


@contextmanager
def reserve(root, gpu_ids, lock, contained):
    with ExitStack() as stack:
        for gpu_id in sorted(set(gpu_ids)):
            if type(gpu_id) is not int or gpu_id < 0:
                raise ValueError("GPU IDs must be nonnegative integers.")
            stack.enter_context(lock(contained(root, f".research/gpu-locks/gpu-{gpu_id}.lock", "GPU lock")))
        yield
