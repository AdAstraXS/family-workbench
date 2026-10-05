"""Correct only the two reviewed legacy PMI labels; retain revision evidence.

NBS publishes seasonally adjusted PMI (see the official explanatory note at
https://www.stats.gov.cn/zwfwck/sjfb/202605/t20260531_1963824.html).
The source, values, units and statistical populations are unchanged.
"""
import hashlib
import json

from django.db import migrations


REVIEWED = {
    "PMI_MANUFACTURING": "3f921fc38cdaffa5c645704bc6dd5ad2b55fe9890c28d0d631b06e1859d39d41",
    "PMI_NONMANUFACTURING": "3636b1b0480d08dc46228264da4c6a876dd563149a7eef13f56ff1434df9bc26",
}


def digest(definition):
    return hashlib.sha256(json.dumps(definition, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def correct_reviewed_pmi(apps, schema_editor):
    Mapping = apps.get_model("macro", "MacroSourceMapping")
    mappings = Mapping.objects.using(schema_editor.connection.alias).filter(
        indicator__country="CN", indicator__code__in=REVIEWED).select_related("indicator")
    for mapping in mappings:
        code = mapping.indicator.code
        legacy = {**mapping.definition, "seasonal": "未季调"}
        corrected = {**legacy, "seasonal": "季调"}
        corrected_hash = digest(corrected)
        # An unreviewed definition must still fail closed, even if its stored
        # hash happens to match. New installations already use the correct label.
        if (digest(legacy) != REVIEWED[code]
                or mapping.definition_hash != digest(mapping.definition)
                or mapping.definition_hash not in {REVIEWED[code], corrected_hash}):
            raise RuntimeError(f"PMI definition requires review: {code}")
        if mapping.definition_hash != corrected_hash:
            mapping.definition = corrected
            mapping.definition_hash = corrected_hash
            mapping.save(update_fields=["definition", "definition_hash"])
    # Observation revisions are immutable evidence. The next ordinary import
    # records the corrected definition without rewriting earlier evidence.


class Migration(migrations.Migration):
    dependencies = [("macro", "0003_macromaintenancerun_macroofficialreport_and_more")]
    operations = [migrations.RunPython(correct_reviewed_pmi, migrations.RunPython.noop)]
