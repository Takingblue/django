from collections import defaultdict, OrderedDict
import os
import sys
import warnings

from django.core.exceptions import ImproperlyConfigured
from django.utils import lru_cache
from django.utils.module_loading import import_lock
from django.utils._os import upath

from .base import AppConfig


class Apps(object):
    """
    A registry that stores the configuration of installed applications.

    It also keeps track of models eg. to provide reverse-relations.
    """

    def __init__(self, installed_apps=()):
        # installed_apps is set to None when creating the master registry
        # because it cannot be populated at that point. Other registries must
        # provide a list of installed apps and are populated immediately.
        if installed_apps is None and hasattr(sys.modules[__name__], 'apps'):
            raise RuntimeError("You must supply an installed_apps argument.")

        # Mapping of app labels => model names => model classes. Every time a
        # model is imported, ModelBase.__new__ calls apps.register_model which
        # creates an entry in all_models. All imported models are registered,
        # regardless of whether they're defined in an installed application
        # and whether the registry has been populated. Since it isn't possible
        # to reimport a module safely (it could reexecute initialization code)
        # all_models is never overridden or reset.
        self.all_models = defaultdict(OrderedDict)

        # Mapping of labels to AppConfig instances for installed apps.
        self.app_configs = OrderedDict()

        # Stack of app_configs. Used to store the current state in
        # set_available_apps and set_installed_apps.
        self.stored_app_configs = []

        # Whether the registry is populated.
        self.ready = False

        # Pending lookups for lazy relations.
        self._pending_lookups = {}

        # Populate apps and models, unless it's the master registry.
        if installed_apps is not None:
            self.populate(installed_apps)

    def populate(self, installed_apps=None):
        """
        Loads application configurations and models.

        This method imports each application module and then each model module.

        It is thread safe and idempotent, but not reentrant.
        """
        if self.ready:
            return
        # Since populate() may be a side effect of imports, and since it will
        # itself import modules, an ABBA deadlock between threads would be
        # possible if we didn't take the import lock. See #18251.
        with import_lock():
            if self.ready:
                return

            # app_config should be pristine, otherwise the code below won't
            # guarantee that the order matches the order in INSTALLED_APPS.
            if self.app_configs:
                raise RuntimeError("populate() isn't reentrant")

            # Load app configs and app modules.
            for entry in installed_apps:
                if isinstance(entry, AppConfig):
                    app_config = entry
                else:
                    app_config = AppConfig.create(entry)
                self.app_configs[app_config.label] = app_config

            # Load models.
            for app_config in self.app_configs.values():
                all_models = self.all_models[app_config.label]
                app_config.import_models(all_models)

            self.clear_cache()
            self.ready = True

            for app_config in self.get_app_configs():
                app_config.setup()

    def check_ready(self):
        """
        Raises an exception if the registry isn't ready.
        """
        if not self.ready:
            raise RuntimeError(
                "App registry isn't populated yet. "
                "Have you called django.setup()?")

    def get_app_configs(self):
        """
        Imports applications and returns an iterable of app configs.
        """
        self.check_ready()
        return self.app_configs.values()

    def get_app_config(self, app_label):
        """
        Imports applications and returns an app config for the given label.

        Raises LookupError if no application exists with this label.
        """
        self.check_ready()
        try:
            return self.app_configs[app_label]
        except KeyError:
            raise LookupError("No installed app with label '%s'." % app_label)

    # This method is performance-critical at least for Django's test suite.
    @lru_cache.lru_cache(maxsize=None)
    def get_models(self, app_mod=None, include_auto_created=False,
                   include_deferred=False, include_swapped=False):
        """
        Returns a list of all installed models.

        By default, the following models aren't included:

        - auto-created models for many-to-many relations without
          an explicit intermediate table,
        - models created to satisfy deferred attribute queries,
        - models that have been swapped out.

        Set the corresponding keyword argument to True to include such models.
        """
        self.check_ready()
        if app_mod:
            warnings.warn(
                "The app_mod argument of get_models is deprecated.",
                PendingDeprecationWarning, stacklevel=2)
            app_label = app_mod.__name__.split('.')[-2]
            try:
                return list(self.get_app_config(app_label).get_models(
                    include_auto_created, include_deferred, include_swapped))
            except LookupError:
                return []

        result = []
        for app_config in self.app_configs.values():
            result.extend(list(app_config.get_models(
                include_auto_created, include_deferred, include_swapped)))
        return result

    def get_model(self, app_label, model_name):
        """
        Returns the model matching the given app_label and model_name.

        model_name is case-insensitive.

        Raises LookupError if no application exists with this label, or no
        model exists with this name in the application.
        """
        self.check_ready()
        return self.get_app_config(app_label).get_model(model_name.lower())

    def register_model(self, app_label, model):
        # Since this method is called when models are imported, it cannot
        # perform imports because of the risk of import loops. It mustn't
        # call get_app_config().
        model_name = model._meta.model_name
        app_models = self.all_models[app_label]
        # Defensive check for extra safety.
        if model_name in app_models:
            raise RuntimeError(
                "Conflicting '%s' models in application '%s': %s and %s." %
                (model_name, app_label, app_models[model_name], model))
        app_models[model_name] = model
        self.clear_cache()

    def has_app(self, app_name):
        """
        Checks whether an application with this name exists in the registry.

        app_name is the full name of the app eg. 'django.contrib.admin'.

        It's safe to call this method at import time, even while the registry
        is being populated. It returns False for apps that aren't loaded yet.
        """
        app_config = self.app_configs.get(app_name.rpartition(".")[2])
        return app_config is not None and app_config.name == app_name

    def get_registered_model(self, app_label, model_name):
        """
        Similar to get_model(), but doesn't require that an app exists with
        the given app_label.

        It's safe to call this method at import time, even while the registry
        is being populated.
        """
        model = self.all_models[app_label].get(model_name.lower())
        if model is None:
            raise LookupError(
                "Model '%s.%s' not registered." % (app_label, model_name))
        return model

    def set_available_apps(self, available):
        """
        Restricts the set of installed apps used by get_app_config[s].

        available must be an iterable of application names.

        set_available_apps() must be balanced with unset_available_apps().

        Primarily used for performance optimization in TransactionTestCase.

        This method is safe is the sense that it doesn't trigger any imports.
        """
        available = set(available)
        installed = set(app_config.name for app_config in self.get_app_configs())
        if not available.issubset(installed):
            raise ValueError("Available apps isn't a subset of installed "
                "apps, extra apps: %s" % ", ".join(available - installed))

        self.stored_app_configs.append(self.app_configs)
        self.app_configs = OrderedDict(
            (label, app_config)
            for label, app_config in self.app_configs.items()
            if app_config.name in available)
        self.clear_cache()

    def unset_available_apps(self):
        """
        Cancels a previous call to set_available_apps().
        """
        self.app_configs = self.stored_app_configs.pop()
        self.clear_cache()

    def set_installed_apps(self, installed):
        """
        Enables a different set of installed apps for get_app_config[s].

        installed must be an iterable in the same format as INSTALLED_APPS.

        set_installed_apps() must be balanced with unset_installed_apps(),
        even if it exits with an exception.

        Primarily used as a receiver of the setting_changed signal in tests.

        This method may trigger new imports, which may add new models to the
        registry of all imported models. They will stay in the registry even
        after unset_installed_apps(). Since it isn't possible to replay
        imports safely (eg. that could lead to registering listeners twice),
        models are registered when they're imported and never removed.
        """
        self.check_ready()
        self.stored_app_configs.append(self.app_configs)
        self.app_configs = OrderedDict()
        self.ready = False
        self.clear_cache()
        self.populate(installed)

    def unset_installed_apps(self):
        """
        Cancels a previous call to set_installed_apps().
        """
        self.app_configs = self.stored_app_configs.pop()
        self.ready = True
        self.clear_cache()

    def clear_cache(self):
        """
        Clears all internal caches, for methods that alter the app registry.

        This is mostly used in tests.
        """
        self.get_models.cache_clear()

    ### DEPRECATED METHODS GO BELOW THIS LINE ###

    def load_app(self, app_name):
        """
        Loads the app with the provided fully qualified name, and returns the
        model module.
        """
        warnings.warn(
            "load_app(app_name) is deprecated.",
            PendingDeprecationWarning, stacklevel=2)
        app_config = AppConfig.create(app_name)
        app_config.import_models(self.all_models[app_config.label])
        self.app_configs[app_config.label] = app_config
        self.clear_cache()
        return app_config.models_module

    def app_cache_ready(self):
        warnings.warn(
            "app_cache_ready() is deprecated in favor of the ready property.",
            PendingDeprecationWarning, stacklevel=2)
        return self.ready

    def get_app(self, app_label):
        """
        Returns the module containing the models for the given app_label.
        """
        warnings.warn(
            "get_app_config(app_label).models_module supersedes get_app(app_label).",
            PendingDeprecationWarning, stacklevel=2)
        try:
            models_module = self.get_app_config(app_label).models_module
        except LookupError as exc:
            # Change the exception type for backwards compatibility.
            raise ImproperlyConfigured(*exc.args)
        if models_module is None:
            raise ImproperlyConfigured(
                "App '%s' doesn't have a models module." % app_label)
        return models_module

    def get_apps(self):
        """
        Returns a list of all installed modules that contain models.
        """
        warnings.warn(
            "[a.models_module for a in get_app_configs()] supersedes get_apps().",
            PendingDeprecationWarning, stacklevel=2)
        app_configs = self.get_app_configs()
        return [app_config.models_module for app_config in app_configs
                if app_config.models_module is not None]

    def _get_app_package(self, app):
        return '.'.join(app.__name__.split('.')[:-1])

    def get_app_package(self, app_label):
        warnings.warn(
            "get_app_config(label).name supersedes get_app_package(label).",
            PendingDeprecationWarning, stacklevel=2)
        return self._get_app_package(self.get_app(app_label))

    def _get_app_path(self, app):
        if hasattr(app, '__path__'):        # models/__init__.py package
            app_path = app.__path__[0]
        else:                               # models.py module
            app_path = app.__file__
        return os.path.dirname(upath(app_path))

    def get_app_path(self, app_label):
        warnings.warn(
            "get_app_config(label).path supersedes get_app_path(label).",
            PendingDeprecationWarning, stacklevel=2)
        return self._get_app_path(self.get_app(app_label))

    def get_app_paths(self):
        """
        Returns a list of paths to all installed apps.

        Useful for discovering files at conventional locations inside apps
        (static files, templates, etc.)
        """
        warnings.warn(
            "[a.path for a in get_app_configs()] supersedes get_app_paths().",
            PendingDeprecationWarning, stacklevel=2)
        self.check_ready()
        app_paths = []
        for app in self.get_apps():
            app_paths.append(self._get_app_path(app))
        return app_paths

    def register_models(self, app_label, *models):
        """
        Register a set of models as belonging to an app.
        """
        warnings.warn(
            "register_models(app_label, *models) is deprecated.",
            PendingDeprecationWarning, stacklevel=2)
        for model in models:
            self.register_model(app_label, model)


apps = Apps(installed_apps=None)
