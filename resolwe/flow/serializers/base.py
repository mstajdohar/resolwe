"""Resolwe base serializer."""
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.fields import empty

from resolwe.rest.serializers import SelectiveFieldMixin

from .contributor import ContributorSerializer


class NoContentError(APIException):
    """Content has not changed exception."""

    status_code = status.HTTP_204_NO_CONTENT
    detail = 'The content has not changed'


class ResolweBaseSerializer(SelectiveFieldMixin, serializers.ModelSerializer):
    """Base serializer for all `Resolwe` objects.

    This class is inherited from `django_rest_framework`'s
    `ModelSerialzer` class. The difference is that
    `update_protected_fields` are removed from `data` dict when update
    is performed.

    To check whether the class is called to create an instance or
    to update an existing one, it checks its value. If the value is
    `None`, a new instance is being created.
    The `update_protected_fields` tuple can be defined in the `Meta`
    class of child class.

    `NoContentError` is raised if no data would be changed, so we
    prevent changing `modified` field.

    """

    contributor = ContributorSerializer()
    name = serializers.CharField(required=False)
    slug = serializers.CharField(required=False)

    def __init__(self, instance=None, data=empty, **kwargs):
        """Initialize attributes."""
        if (instance is not None and data is not empty
                and hasattr(self.Meta, 'update_protected_fields')):  # pylint: disable=no-member
            for field in self.Meta.update_protected_fields:  # pylint: disable=no-member
                if field in data:
                    data.pop(field)

            # prevent modification if there are no updates
            if set(data.keys()).issubset(set(self.Meta.read_only_fields)):  # pylint: disable=no-member
                raise NoContentError()

        super().__init__(instance, data, **kwargs)

    @property
    def request(self):
        """Extract request object from serializer context."""
        return self.context.get('request', None)
