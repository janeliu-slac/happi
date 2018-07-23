"""
Functions to instantiate the Python representations of happi Containers
"""
import sys
import types
import logging
import importlib
import asyncio
from functools import partial
from multiprocessing.pool import ThreadPool

from jinja2 import Environment, meta

from .utils import create_alias

logger = logging.getLogger(__name__)

cache = dict()
main_event_loop = None


def fill_template(template, device, enforce_type=False):
    """
    Fill a Jinja2 template using information from a device

    Parameters
    ----------
    template : str
        Jinja2 template

    device : happi.Device
        Any device container

    enforce_type : bool, optional
        Force the output of the rendered template to match the enforced type of
        the happi information that was used to fill it.
    """
    # Create a template and render our happi information inside it
    env = Environment().from_string(template)
    filled = env.render(**device.post())
    # Find which variable we used in the template, get the type and convert
    # our rendered template to agree with this
    info = meta.find_undeclared_variables(env.environment.parse(template))
    if len(info) == 1 and enforce_type:
        # Get the original attribute back from the device. If this does not
        # exist there is a possibility it is a piece of metadata e.t.c
        try:
            attr_name = info.pop()
            typed_attr = getattr(device, attr_name)
        except AttributeError:
            logger.warning("Can not enforce type to match attribute %s",
                           attr_name)
            return filled
        # If this was a straight substitution with nothing else in the template
        # we can just return the attribute itself thus preserving type
        if str(typed_attr) == filled:
            filled = typed_attr
        # If there is something more complex going on we can attempt to convert
        # it to match the type of the original
        else:
            attr_type = type(typed_attr)
            try:
                filled = attr_type(filled)
            except ValueError:
                logger.exception("Unable to convert %s to %s",
                                 filled, attr_type)
    return filled


def from_container(device, attach_md=True, use_cache=True, threaded=False):
    """
    Load a device from a happi container

    The container is queried for the device_class, args and kwargs. Then if the
    associated package is not already loaded it is imported. The specified
    class is then instantiated with the given args and kwargs provided.

    This function does not attempt to catch exceptions either during module
    imports or device creation. If you would like a series of independent
    devices to be loaded use :func:`.load_devices`.

    By default, the instantiated object has the original container added on as
    ``.md``. This allows applications to utilize additional metadata
    information that may not be included in the basic class constructor. On
    later calls, the container you request is checked against this stored
    metadata. If a discrepancy is found the object is **forced** to reload, not
    retrieved from the cache.

    Parameters
    ----------
    device : happi.Device

    attach_md: bool, optional
        Attach the container to the instantiated object as `md`

    use_cache: bool, optional
        When devices are loaded they are stored in the ``happi.cache``
        dictionary. This means that repeated attempts to load the device will
        return the same object. This prevents unnecessary EPICS connections
        from being initialized in the same process. If a new object is
        needed, set `use_cache` to False and a new object will be created,
        overriding the current cached object. An object with matching prefix
        and differing metadata will always return a new instantiation of the
        device.

    threaded: bool, optional
        Set this to True when calling inside a thread.

    Returns
    -------
    obj : happi.Device.device_class
    """
    # We sync with the main thread's loop so that they work as expected later
    if threaded:
        asyncio.set_event_loop(main_event_loop)

    # Return a cached version of the device if present and not forced
    if use_cache and device.prefix in cache:
        cached_device = cache[device.prefix]
        # If the metadata has not been modified or we can't review it.
        # Return the cached object
        if not hasattr(cached_device, 'md') or cached_device.md == device:
            logger.debug("Loading %s from cache ...", device.prefix)
            return cached_device
        # Otherwise reload
        else:
            logger.warning("Device %s has already been loaded, but the "
                           "database information has been modified. "
                           "Reloading ...", device.prefix)

    # Find the class and module of the container.
    if not device.device_class:
        raise ValueError("Device %s does not have an associated Python class",
                         device.name)
    mod, cls = device.device_class.rsplit('.', 1)
    # Import the module if not already present
    # Otherwise use the stashed version in sys.modules
    if mod in sys.modules:
        logger.debug("Using previously imported version of %s", mod)
        mod = sys.modules[mod]
    else:
        logger.debug("Importing %s", mod)
        mod = importlib.import_module(mod)
    # Gather our device class from the given module
    try:
        cls = getattr(mod, cls)
    except AttributeError as exc:
        raise ImportError("Unable to import %s from %s" %
                          (cls, mod.__name__)) from exc

    # Create correctly typed arguments from happi information
    def create_arg(arg):
        if not isinstance(arg, str):
            return arg
        return fill_template(arg, device, enforce_type=True)

    # Treat all our args and kwargs as templates
    args = [create_arg(arg) for arg in device.args]
    kwargs = dict((key, create_arg(val))
                  for key, val in device.kwargs.items())
    # Return the instantiated device
    obj = cls(*args, **kwargs)
    # Attach the metadata to the object
    if attach_md:
        try:
            setattr(obj, 'md', device)
        except Exception as exc:
            logger.warning("Unable to attach metadata dictionary to device")

    # Store a copy of the device in the cache
    cache[device.prefix] = obj
    return obj


def load_devices(*devices, pprint=False, namespace=None, use_cache=True,
                 threaded=True, **kwargs):
    """
    Load a series of devices into a namespace

    Parameters
    ----------
    args :
        List of happi containers to load

    pprint: bool, optional
        Print results of device loads

    namespace : obj, optional
        Namespace to collect loaded devices in. By default this will be a
        ``types.SimpleNamespace``

    use_cache : bool, optional
        If set to ``False``, we'll ignore the cache and always make new
        devices.

    threaded : bool, optional
        Defaults to True to create each device in a background thread.
        Note that this assumes that no two devices in the *devices input are
        the same device. You are not guaranteed to load from the cache
        correctly if you ask for the same device to be loaded twice in the same
        threaded load.

    kwargs:
        Are passed to :func:`.from_container`
    """
    # Create our namespace if we were not given one
    namespace = namespace or types.SimpleNamespace()
    name_list = [container.name for container in devices]
    if threaded:
        pool = ThreadPool(len(devices))
        opt_load = partial(load_device, pprint=pprint, use_cache=use_cache,
                           threaded=True, **kwargs)
        loaded_list = pool.map(opt_load, devices)
    else:
        loaded_list = []
        for device in devices:
            loaded = load_device(device, pprint=pprint, use_cache=use_cache,
                                 threaded=False, **kwargs)
            loaded_list.append(loaded)
    for dev, name in zip(loaded_list, name_list):
        attr = create_alias(name)
        setattr(namespace, attr, dev)
    return namespace


def load_device(device, pprint=False, threaded=False, **kwargs):
    # Attempt to load our device. If this raises an exception
    # catch and store it so we can easily view the traceback
    # later without going to logs, e.t.c
    logger.debug("Loading device %s ...", device.name)
    load_message = "Loading %s [%s] ... "
    success = "\033[32mSUCCESS\033[0m!"
    failed = "\033[31mFAILED\033[0m"
    if pprint:
        device_message = load_message % (device.name, device.device_class)
        if not threaded:
            print(device_message, end='')
    try:
        loaded = from_container(device, **kwargs)
        logger.info(load_message + success,
                    device.name, device.device_class)
        if pprint:
            if threaded:
                print(device_message + success)
            else:
                print(success)
    except Exception as exc:
        if pprint:
            if threaded:
                print(device_message + failed)
            else:
                print(failed)
        logger.exception('Error loading %s', device.name)
        loaded = exc
    return loaded
