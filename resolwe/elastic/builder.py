""".. Ignore pydocstyle D400.

=====================
Elastic Index Builder
=====================

"""
import inspect
import os
import re
import uuid
from importlib import import_module

from django.apps import apps
from django.db import models
from django.db.models.fields.related_descriptors import ManyToManyDescriptor
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete

from resolwe.test.utils import is_testing

from .composer import composer
from .indices import BaseIndex
from .utils import prepare_connection

__all__ = (
    'index_builder',
    'ManyToManyDependency',
)

# UUID used in tests to make sure that no index is re-used.
TESTING_UUID = str(uuid.uuid4())


class BuildArgumentsCache:
    """Cache for storing arguments for index builder.

    If set value contains a queryset, it is evaluated and later
    recreated to prevent influence from database changes.

    """

    def __init__(self):
        """Initialize cache."""
        self._cache = {}

    def _get_cache_key(self, obj):
        """Derive cache key for given object."""
        if obj is not None:
            return obj.pk

        return obj

    def _clean_cache(self, obj):
        """Clean cache."""
        del self._cache[self._get_cache_key(obj)]

    def set(self, obj, build_kwargs):
        """Set cached value."""
        assert self._get_cache_key(obj) not in self._cache

        if build_kwargs is None:
            build_kwargs = {}

        cached = {}
        if 'queryset' in build_kwargs:
            cached = {
                'model': build_kwargs['queryset'].model,
                'pks': list(build_kwargs['queryset'].values_list('pk', flat=True)),
            }

        elif 'obj' in build_kwargs:
            cached = {
                'obj': build_kwargs['obj'],
            }

        self._cache[self._get_cache_key(obj)] = cached

    def take(self, obj):
        """Get cached value and clean cache."""
        cached = self._cache[self._get_cache_key(obj)]
        build_kwargs = {}

        if 'model' in cached and 'pks' in cached:
            build_kwargs['queryset'] = cached['model'].objects.filter(pk__in=cached['pks'])

        elif 'obj' in cached:
            if cached['obj'].__class__.objects.filter(pk=cached['obj'].pk).exists():
                build_kwargs['obj'] = cached['obj']
            else:
                # Object was deleted in the meantime.
                build_kwargs['queryset'] = cached['obj'].__class__.objects.none()

        self._clean_cache(obj)

        return build_kwargs


class ElasticSignal:
    """Class for creating signals to update/create indexes.

    To register the signal, add the following code::

        signal = ElasticSignal(<my_signal>, <method_name>)
        signal.connect(<signal_type>, [sender=<my_model>])

    You may later disconnect the signal by calling::

        signal.disconnect()

    ``signal type`` can be i.e. ``django.db.models.signals.pre_save``.

    """

    def __init__(self, index, method_name, pass_kwargs=False):
        """Initialize signal."""
        self.index = index
        self.method_name = method_name
        self.pass_kwargs = pass_kwargs
        self.connections = []

    def connect(self, signal, **kwargs):
        """Connect a specific signal type to this receiver."""
        signal.connect(self, **kwargs)
        self.connections.append((signal, kwargs))

    def disconnect(self):
        """Disconnect all connected signal types from this receiver."""
        for signal, kwargs in self.connections:
            signal.disconnect(self, **kwargs)

    def __call__(self, sender, instance, **kwargs):
        """Process signal."""
        method = getattr(self.index, self.method_name)
        if self.pass_kwargs:
            method(obj=instance, **kwargs)
        else:
            method(obj=instance)


class Dependency:
    """Abstract base class for index model dependencies."""

    def __init__(self, model):
        """Construct dependency."""
        self.model = model
        self.index = None

    def connect(self, index):
        """Connect signals needed for dependency updates.

        Pre- and post-delete signals have to be handled separately, as:

          * in the pre-delete signal we have the information which
            objects to rebuild, but affected relations are still
            presented, so rebuild would reflect in the wrong (outdated)
            indices
          * in the post-delete signal indices can be rebuild corectly,
            but there is no information which objects to rebuild, as
            affected relations were already deleted

        To bypass this, list of objects should be stored in the
        pre-delete signal indexing should be triggered in the
        post-delete signal.
        """
        self.index = index

        signal = ElasticSignal(self, 'process', pass_kwargs=True)
        signal.connect(post_save, sender=self.model)
        signal.connect(pre_delete, sender=self.model)

        pre_delete_signal = ElasticSignal(self, 'process_predelete', pass_kwargs=True)
        pre_delete_signal.connect(pre_delete, sender=self.model)

        post_delete_signal = ElasticSignal(self, 'process_delete', pass_kwargs=True)
        post_delete_signal.connect(post_delete, sender=self.model)

        return [signal, pre_delete_signal, post_delete_signal]

    def process(self, obj, **kwargs):
        """Process signals from dependencies."""
        raise NotImplementedError

    def process_predelete(self, obj, **kwargs):
        """Process signals from dependencies."""
        raise NotImplementedError

    def process_delete(self, obj, **kwargs):
        """Process signals from dependencies."""
        raise NotImplementedError


class ManyToManyDependency(Dependency):
    """Dependency on a many-to-many relation."""

    def __init__(self, field):
        """Construct m2m dependency."""
        # Determine which model is the target model as either side of the relation
        # may be passed as `field`.
        if field.reverse:
            model = field.rel.related_model
            self.accessor = field.rel.field.attname
        else:
            model = field.rel.model
            if field.rel.symmetrical:
                # Symmetrical m2m relation on self has no reverse accessor.
                raise NotImplementedError(
                    'Dependencies on symmetrical M2M relations are not supported due '
                    'to strange handling of the m2m_changed signal which only makes '
                    'half of the relation visible during signal execution. For now you '
                    'need to use symmetrical=False on the M2M field definition.'
                )
            else:
                self.accessor = field.rel.get_accessor_name()

        super().__init__(model)
        self.field = field
        # Cache for pre/post-delete signals.
        self.delete_cache = BuildArgumentsCache()
        # Cache for pre/post-remove action in m2m_changed signal.
        self.remove_cache = BuildArgumentsCache()

    def connect(self, index):
        """Connect signals needed for dependency updates."""
        signals = super().connect(index)

        m2m_signal = ElasticSignal(self, 'process_m2m', pass_kwargs=True)
        m2m_signal.connect(m2m_changed, sender=self.field.through)
        signals.append(m2m_signal)

        # If the relation has a custom through model, we need to subscribe to it.
        if not self.field.rel.through._meta.auto_created:  # pylint: disable=protected-access
            signal = ElasticSignal(self, 'process_m2m_through_save', pass_kwargs=True)
            signal.connect(post_save, sender=self.field.rel.through)
            signals.append(signal)

            signal = ElasticSignal(self, 'process_m2m_through_pre_delete', pass_kwargs=True)
            signal.connect(pre_delete, sender=self.field.rel.through)
            signals.append(signal)

            signal = ElasticSignal(self, 'process_m2m_through_post_delete', pass_kwargs=True)
            signal.connect(post_delete, sender=self.field.rel.through)
            signals.append(signal)

        return signals

    def filter(self, obj, update_fields=None):
        """Determine if dependent object should be processed.

        If ``False`` is returned, processing of the dependent object will
        be aborted.
        """
        pass

    def _filter(self, objects, **kwargs):
        """Determine if dependent object should be processed."""
        for obj in objects:
            if self.filter(obj, **kwargs) is False:
                return False

        return True

    def _get_build_kwargs(self, obj, pk_set=None, action=None, update_fields=None, reverse=None, **kwargs):
        """Prepare arguments for rebuilding indices."""
        # Invert the meaning of reverse for fields which are already reverse descriptors.
        if reverse is not None and self.field.reverse:
            reverse = not reverse
        elif reverse is None:
            reverse = True

        if reverse:
            # Check filter before rebuilding index.
            if not self._filter([obj], update_fields=update_fields):
                return

            queryset = getattr(obj, self.accessor).all()
            return {'queryset': queryset}
        else:
            if action != 'post_clear':
                # Check filter before rebuilding index.
                if not self._filter(self.model.objects.filter(pk__in=pk_set)):
                    return

            return {'obj': obj}

    def process_predelete(self, obj, pk_set=None, action=None, update_fields=None, **kwargs):
        """Render the queryset of influenced objects and cache it."""
        build_kwargs = self._get_build_kwargs(obj, pk_set, action, update_fields, **kwargs)
        self.delete_cache.set(obj, build_kwargs)

    def process_delete(self, obj, pk_set=None, action=None, update_fields=None, **kwargs):
        """Recreate queryset from the index and rebuild the index."""
        build_kwargs = self.delete_cache.take(obj)

        if build_kwargs:
            self.index.build(**build_kwargs)

    def process_m2m(self, obj, pk_set=None, action=None, update_fields=None, cache_key=None, **kwargs):
        """Process signals from dependencies.

        Remove signal is processed in two parts. For details see:
        :func:`~Dependency.connect`
        """
        if action not in (None, 'post_add', 'pre_remove', 'post_remove', 'post_clear'):
            return

        if action == 'post_remove':
            build_kwargs = self.remove_cache.take(cache_key)
        else:
            build_kwargs = self._get_build_kwargs(obj, pk_set, action, update_fields, **kwargs)

        if action == 'pre_remove':
            self.remove_cache.set(cache_key, build_kwargs)
            return

        if build_kwargs:
            self.index.build(**build_kwargs)

    def _process_m2m_through(self, obj, action):
        """Process custom M2M through model actions."""
        source = getattr(obj, self.field.rel.field.m2m_field_name())
        target = getattr(obj, self.field.rel.field.m2m_reverse_field_name())

        pk_set = set()
        if target:
            pk_set.add(target.pk)

        self.process_m2m(source, pk_set, action=action, reverse=False, cache_key=obj)

    def process_m2m_through_save(self, obj, created=False, **kwargs):
        """Process M2M post save for custom through model."""
        # We are only interested in signals that establish relations.
        if not created:
            return

        self._process_m2m_through(obj, 'post_add')

    def process_m2m_through_pre_delete(self, obj, **kwargs):
        """Process M2M pre delete for custom through model."""
        self._process_m2m_through(obj, 'pre_remove')

    def process_m2m_through_post_delete(self, obj, **kwargs):
        """Process M2M post delete for custom through model."""
        self._process_m2m_through(obj, 'post_remove')

    def process(self, obj, pk_set=None, action=None, update_fields=None, **kwargs):
        """Process signals from dependencies."""
        build_kwargs = self._get_build_kwargs(obj, pk_set, action, update_fields, **kwargs)

        if build_kwargs:
            self.index.build(**build_kwargs)


class IndexBuilder:
    """Find indexes and register corresponding signals.

    Indexes are auto collected from ``elastic_indexes.py`` files from
    all Django registered apps

    Post-save and pre-delete signals are registered for objects
    specified in ``object_type`` attribute of each index.

    """

    def __init__(self):
        """Initialize index builder object."""
        #: list of index builders
        self.indexes = []

        #: list of registered signals
        self.signals = []

        prepare_connection()

        self.discover_indexes()
        self.register_signals()

    def _connect_signal(self, index):
        """Create signals for building indexes."""
        post_save_signal = ElasticSignal(index, 'build')
        post_save_signal.connect(post_save, sender=index.object_type)
        self.signals.append(post_save_signal)

        post_delete_signal = ElasticSignal(index, 'remove_object')
        post_delete_signal.connect(post_delete, sender=index.object_type)
        self.signals.append(post_delete_signal)

        # Connect signals for all dependencies.
        for dependency in index.get_dependencies():
            # Automatically convert m2m fields to dependencies.
            if isinstance(dependency, (models.ManyToManyField, ManyToManyDescriptor)):
                dependency = ManyToManyDependency(dependency)
            elif not isinstance(dependency, Dependency):
                raise TypeError("Unsupported dependency type: {}".format(repr(dependency)))

            signal = dependency.connect(index)
            self.signals.extend(signal)

    def unregister_signals(self):
        """Delete signals for building indexes."""
        for signal in self.signals:
            signal.disconnect()
        self.signals = []

    def register_signals(self):
        """Register signals for all indexes."""
        for index in self.indexes:
            if index.object_type:
                self._connect_signal(index)

    def create_mappings(self):
        """Create mappings for all indexes."""
        for index in self.indexes:
            index.create_mapping()

    def discover_indexes(self):
        """Save list of index builders into ``_index_builders``."""
        self.indexes = []

        for app_config in apps.get_app_configs():
            indexes_path = '{}.elastic_indexes'.format(app_config.name)
            try:
                indexes_module = import_module(indexes_path)

                for attr_name in dir(indexes_module):
                    attr = getattr(indexes_module, attr_name)
                    if inspect.isclass(attr) and issubclass(attr, BaseIndex) and attr is not BaseIndex:
                        # Make sure that parallel tests have different indices.
                        if is_testing():
                            index = attr.document_class._doc_type.index  # pylint: disable=protected-access
                            testing_postfix = '_test_{}_{}'.format(TESTING_UUID, os.getpid())

                            if not index.endswith(testing_postfix):
                                # Replace current postfix with the new one.
                                if attr.testing_postfix:
                                    index = index[:-len(attr.testing_postfix)]
                                index = index + testing_postfix
                                attr.testing_postfix = testing_postfix

                            attr.document_class._doc_type.index = index  # pylint: disable=protected-access

                        index = attr()

                        # Apply any extensions defined for the given index. Currently index extensions are
                        # limited to extending "mappings".
                        for extension in composer.get_extensions(attr):
                            mapping = getattr(extension, 'mapping', {})
                            index.mapping.update(mapping)

                        self.indexes.append(index)
            except ImportError as ex:
                if not re.match('No module named .*elastic_indexes.*', str(ex)):
                    raise

    def build(self, obj=None, queryset=None, push=True):
        """Trigger building of the indexes.

        Support passing ``obj`` parameter to the indexes, so we can
        trigger build only for one object.
        """
        for index in self.indexes:
            index.build(obj, queryset, push)

    def push(self, index=None):
        """Push built documents to ElasticSearch.

        If ``index`` is specified, only that index will be pushed.
        """
        for ind in self.indexes:
            if index and not isinstance(ind, index):
                continue
            ind.push()

    def delete(self, skip_mapping=False):
        """Delete all entries from ElasticSearch."""
        for index in self.indexes:
            index.destroy()
            if not skip_mapping:
                index.create_mapping()

    def remove_object(self, obj):
        """Delete given object from all indexes."""
        for index in self.indexes:
            index.remove_object(obj)

    def destroy(self):
        """Delete all indexes from Elasticsearch and index builder."""
        self.unregister_signals()
        for index in self.indexes:
            index.destroy()
        self.indexes = []


index_builder = IndexBuilder()  # pylint: disable=invalid-name
