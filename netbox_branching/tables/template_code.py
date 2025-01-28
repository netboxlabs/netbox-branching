REPLAY_CHANGE = """
<a href="{% url 'plugins:netbox_branching:branch_replay' pk=object.pk %}?start={{ record.pk }}" class="btn btn-sm btn-primary" title="Replay from here">
  <i class="mdi mdi-replay"></i>
</a>
"""
