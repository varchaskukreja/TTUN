"""Preserve per-test identity across reusable process-pool workers."""
def run(context, function, args, kwargs):
    import sitecustomize
    if hasattr(sitecustomize, '_cov'):
        sitecustomize._context = context
        sitecustomize._cov.switch_context(context)
    return function(*args, **kwargs)
