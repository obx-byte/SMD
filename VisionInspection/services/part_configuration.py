"""Validation helpers for part-specific vision configuration."""

from VisionInspection.models import VisionPartConfiguration


def validate_configuration(configuration, discovered_classes):
    """Check saved class metadata and side assignments against discovered classes."""
    errors = []
    mode = configuration.inspection_mode
    enabled_rule_count = 0
    for association in configuration.part_models.all():
        if not association.is_enabled:
            continue
        actual_names = discovered_classes.get(association.pk)
        if actual_names is None:
            errors.append(f"Model {association.model_id} has no discovered class metadata.")
            continue
        actual_names = list(actual_names)
        for rule in association.class_configs.filter(is_enabled=True):
            enabled_rule_count += 1
            if rule.class_id >= len(actual_names) or actual_names[rule.class_id] != rule.class_name:
                errors.append(f"Class {rule.class_name} no longer matches the discovered model classes.")
            if mode == VisionPartConfiguration.MODE_FRONT_ONLY and rule.side in ("BACK", "BOTH"):
                errors.append(f"Class {rule.class_name} requires Back, but this part is Front Only.")
            elif mode == VisionPartConfiguration.MODE_BACK_ONLY and rule.side in ("FRONT", "BOTH"):
                errors.append(f"Class {rule.class_name} requires Front, but this part is Back Only.")
    if enabled_rule_count == 0:
        errors.append("Enable at least one class rule in an enabled model.")
    return errors