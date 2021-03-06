"""Resolwe process serializer."""
from resolwe.flow.models import Process

from .base import ResolweBaseSerializer


class ProcessSerializer(ResolweBaseSerializer):
    """Serializer for Process objects."""

    class Meta:
        """ProcessSerializer Meta options."""

        model = Process
        read_only_fields = (
            'created',
            'id',
            'modified',
        )
        update_protected_fields = (
            'category',
            'contributor',
            'data_name',
            'description',
            'flow_collection',
            'input_schema',
            'name',
            'output_schema',
            'persistence',
            'requirements',
            'run',
            'scheduling_class',
            'slug',
            'type',
            'version',
        )
        fields = read_only_fields + update_protected_fields
