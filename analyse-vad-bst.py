import json
import csv
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

SRC = "visit-a-domicile.json"  # JSON Lines input file
OUT_VISITS = "validation_visites.csv"
OUT_HH_MONTH = "validation_menage_mois.csv"
OUT_HH = "nonconformites_par_menage.csv"

# ------------------------------------------------------------
# 1) BUSINESS LOGIC - AUTHORIZED THEMES
# ------------------------------------------------------------
# Exact business themes from the rules
BUSINESS_THEMES = [
    "suivi_nouveau_ne_ou_pratiques_familiales_essentielles",
    "vaccination",
    "allaitement_et_alimentation_de_complement",
    "planning_familial",
    "approvisionnement_potabilisation_conservation_eau",
    "evacuation_traitement_dechets",
    "lavage_des_mains",
    "construction_entretien_toilettes_latrines",
    "negociations",
    "prevention_maladies_a_potentiel_epidemique",
]

# ------------------------------------------------------------
# 2) SOURCE -> BUSINESS THEME CROSSWALK
# ------------------------------------------------------------
# Strict mode:
# - Only source codes explicitly mapped below are treated as valid.
# - Any other source code is treated as "theme non mappé / hors critères"
#   unless your program confirms an official crosswalk.
#
# This avoids guessing.
SOURCE_THEME_TO_BUSINESS = {
    # clearly aligned
    "anc_and_cpon_importance": "suivi_nouveau_ne_ou_pratiques_familiales_essentielles",
    "child_vaccination": "vaccination",
    "exclusive_breastfeeding": "allaitement_et_alimentation_de_complement",
    "balanced_nutrition": "allaitement_et_alimentation_de_complement",
    "malaria_prevention": "prevention_maladies_a_potentiel_epidemique",

    # NOTE:
    # The following source codes were observed in the data but are NOT mapped
    # automatically because the business equivalence is not explicit enough:
    # - diarrhea_prevention
    # - early_care_acces
    # - treatment_adherence
    # - child_registration_schooling
    # - early_anc
    # - vitamin_a
    # - gender_violence_prevention
    # - danger_signs_children_women
    # - other
    #
    # Add them here only if you have an official approved mapping.
}

# ------------------------------------------------------------
# 3) REQUIRED RAW REGISTER FIELDS (BUSINESS LOGIC)
# ------------------------------------------------------------
REQUIRED_REGISTER_FIELDS = [
    "numero_menage",
    "nom_chef_famille",
    "adresse",
    "telephone",
    "personnes_ciblees",
    "sujets_discutes",
    "recommandations_faites",
    "prochain_rendez_vous",
    "heure_debut",
    "heure_fin",
    "focus_groupe",
]

# ------------------------------------------------------------
# 4) CONDITIONAL REQUIRED FIELDS
# ------------------------------------------------------------
# Required from the 2nd visit onward within the same household-month
CONDITIONAL_REQUIRED_FIELDS = [
    "revue_recommandations_precedente",
]

# ------------------------------------------------------------
# 5) SOURCE FIELD CANDIDATES
# ------------------------------------------------------------
# These are candidate keys in the JSON schema.
# If none is found, the field is treated as missing.

FIELD_CANDIDATES = {
    "numero_menage": [
        "family_uuid",
        "visited_contact_uuid",
        "inputs_contact__id",
    ],
    "nom_chef_famille": [
        "key_person_met_full_name",
        "inputs_contact_name",
        "muso_patient_name",
    ],
    "adresse": [
        "address",
        "adresse",
        "household_address",
        "contact_address",
    ],
    "telephone": [
        "telephone",
        "phone",
        "contact_phone",
        "household_phone",
        # WARNING: we deliberately do NOT use 'submitter' here,
        # because submitter appears to be the agent phone, not the household phone.
    ],
    "personnes_ciblees": [
        "key_person_met_full_name",
        "inputs_contact_name",
        "muso_patient_name",
    ],
    "recommandations_faites": [
        "recommendations",
        "recommandations",
        "household_recommendations",
    ],
    "prochain_rendez_vous": [
        "next_appointment",
        "next_visit_date",
        "prochain_rendez_vous",
        "next_followup",
    ],
    "revue_recommandations_precedente": [
        "previous_recommendations_review",
        "review_previous_recommendations",
        "revue_recommandations_precedente",
    ],
    "heure_debut": [
        "heure_debut",
        "start_time",
        "talk_start_time",
        "causerie_heure_debut",
    ],
    "heure_fin": [
        "heure_fin",
        "end_time",
        "talk_end_time",
        "causerie_heure_fin",
    ],
    "focus_groupe": [
        "focus_groupe",
        "focus_group",
        "is_focus_group",
    ],
    "agent_id": [
        "submitter",
        "reported_by",
    ],
    "visit_id": [
        "meta_instanceid",
        "xmlforms_uuid",
    ],
}

# ------------------------------------------------------------
# 6) SUPPORT FUNCTIONS
# ------------------------------------------------------------

def nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        s = value.strip()
        return s != "" and s.lower() != "nan"
    if isinstance(value, list):
        return len(value) > 0
    return True

def first_nonempty(obj, keys):
    for key in keys:
        value = obj.get(key)
        if nonempty(value):
            return value
    return None

def normalize_whitespace(s):
    if s is None:
        return None
    return re.sub(r"\s+", " ", str(s)).strip()

def parse_datetime_flexible(value):
    if not nonempty(value):
        return None
    s = normalize_whitespace(value)

    formats = [
        "%Y-%m-%d %H:%M:%S.%f UTC",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def parse_month_from_best_date(obj):
    dt = get_best_visit_datetime(obj)
    if dt:
        return dt.strftime("%Y-%m")
    return "MOIS_INCONNU"

def get_best_visit_datetime(obj):
    # Prefer visited_date if it exists, otherwise reported_date
    dt = parse_datetime_flexible(obj.get("visited_date"))
    if dt:
        return dt
    dt = parse_datetime_flexible(obj.get("reported_date"))
    if dt:
        return dt
    return None

def parse_clock_time(value):
    """
    Accepts strings like:
    - 08:15
    - 08:15:00
    - 2026-01-01 08:15:00
    Returns minutes since midnight or None.
    """
    if not nonempty(value):
        return None

    s = normalize_whitespace(value)

    # Full datetime first
    dt = parse_datetime_flexible(s)
    if dt:
        return dt.hour * 60 + dt.minute

    # HH:MM or HH:MM:SS
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm

    return None

def compute_duration_minutes(start_value, end_value):
    start_min = parse_clock_time(start_value)
    end_min = parse_clock_time(end_value)
    if start_min is None or end_min is None:
        return None

    # Strict interpretation: negative duration is invalid
    if end_min < start_min:
        return None

    return end_min - start_min

def normalize_yes_no(value):
    if not nonempty(value):
        return None
    s = normalize_whitespace(value).lower()
    yes_values = {"yes", "oui", "o", "y", "true", "1"}
    no_values = {"no", "non", "n", "false", "0"}
    if s in yes_values:
        return "Oui"
    if s in no_values:
        return "Non"
    return None

def extract_topics(obj):
    """
    Extracts raw discussed topics from:
    - covered_themes_array (preferred)
    - covered_themes (space-separated string in observed dataset)
    - specify_themes (free text)
    """
    raw_topics = []

    arr = obj.get("covered_themes_array")
    if isinstance(arr, list) and arr:
        raw_topics.extend([normalize_whitespace(x) for x in arr if nonempty(x)])

    raw_str = obj.get("covered_themes")
    if nonempty(raw_str):
        # observed source uses space-separated codes
        raw_topics.extend([normalize_whitespace(x) for x in str(raw_str).split() if nonempty(x)])

    free_text = obj.get("specify_themes")
    if nonempty(free_text):
        raw_topics.append(normalize_whitespace(free_text))

    # keep order, remove duplicates
    seen = set()
    result = []
    for x in raw_topics:
        if x and x not in seen:
            result.append(x)
            seen.add(x)
    return result

def map_topics_to_business(raw_topics):
    mapped = []
    unmapped = []
    for topic in raw_topics:
        mapped_theme = SOURCE_THEME_TO_BUSINESS.get(topic)
        if mapped_theme:
            mapped.append(mapped_theme)
        else:
            unmapped.append(topic)

    # deduplicate mapped themes
    mapped_unique = []
    seen = set()
    for x in mapped:
        if x not in seen:
            mapped_unique.append(x)
            seen.add(x)

    return mapped_unique, unmapped

def extract_register_fields(obj):
    fields = {}
    for field_name, candidates in FIELD_CANDIDATES.items():
        fields[field_name] = first_nonempty(obj, candidates)

    fields["numero_menage"] = normalize_whitespace(fields["numero_menage"])
    fields["nom_chef_famille"] = normalize_whitespace(fields["nom_chef_famille"])
    fields["personnes_ciblees"] = normalize_whitespace(fields["personnes_ciblees"])
    fields["adresse"] = normalize_whitespace(fields["adresse"])
    fields["telephone"] = normalize_whitespace(fields["telephone"])
    fields["recommandations_faites"] = normalize_whitespace(fields["recommandations_faites"])
    fields["prochain_rendez_vous"] = normalize_whitespace(fields["prochain_rendez_vous"])
    fields["revue_recommandations_precedente"] = normalize_whitespace(fields["revue_recommandations_precedente"])
    fields["heure_debut"] = normalize_whitespace(fields["heure_debut"])
    fields["heure_fin"] = normalize_whitespace(fields["heure_fin"])
    fields["focus_groupe"] = normalize_whitespace(fields["focus_groupe"])
    fields["agent_id"] = normalize_whitespace(fields["agent_id"]) or "AGENT_INCONNU"
    fields["visit_id"] = normalize_whitespace(fields["visit_id"])
    return fields

def build_missing_required_fields(fields, sujets_discutes_present):
    missing = []

    if not nonempty(fields["numero_menage"]):
        missing.append("numero_menage")
    if not nonempty(fields["nom_chef_famille"]):
        missing.append("nom_chef_famille")
    if not nonempty(fields["adresse"]):
        missing.append("adresse")
    if not nonempty(fields["telephone"]):
        missing.append("telephone")
    if not nonempty(fields["personnes_ciblees"]):
        missing.append("personnes_ciblees")
    if not sujets_discutes_present:
        missing.append("sujets_discutes")
    if not nonempty(fields["recommandations_faites"]):
        missing.append("recommandations_faites")
    if not nonempty(fields["prochain_rendez_vous"]):
        missing.append("prochain_rendez_vous")
    if not nonempty(fields["heure_debut"]):
        missing.append("heure_debut")
    if not nonempty(fields["heure_fin"]):
        missing.append("heure_fin")
    if normalize_yes_no(fields["focus_groupe"]) is None:
        missing.append("focus_groupe")

    return missing

def build_conditional_missing_fields(fields, visit_order_in_month):
    missing = []
    if visit_order_in_month >= 2:
        if not nonempty(fields["revue_recommandations_precedente"]):
            missing.append("revue_recommandations_precedente")
    return missing

def top_issues(counter_obj, topn=5):
    labels = [
        ("visites_theme_hors_criteres", "theme hors critères"),
        ("visites_sans_sujet", "sujet discuté manquant"),
        ("visites_champs_obligatoires_manquants", "champs obligatoires manquants"),
        ("visites_revue_precedente_manquante", "revue précédente manquante"),
        ("visites_duree_non_valide", "durée hors norme ou non vérifiable"),
        ("visites_focus_groupe_non_renseigne", "focus groupe non renseigné"),
        ("visites_dans_mois_lt2", "moins de 2 visites dans le mois"),
    ]
    items = []
    for key, label in labels:
        v = counter_obj.get(key, 0)
        if v:
            items.append((v, label))
    items.sort(reverse=True)
    return " | ".join([f"{label}: {value}" for value, label in items[:topn]]) or "aucune"

# ============================================================
# PASS 1 - VISIT ORDER WITHIN HOUSEHOLD-MONTH
# ============================================================

visits_by_household_month = defaultdict(list)
household_agents = defaultdict(set)

with open(SRC, "r", encoding="utf-8") as f:
    for line_no, line in enumerate(f, start=1):
        line = line.strip()
        if not line:
            continue

        obj = json.loads(line)
        fields = extract_register_fields(obj)

        household_id = fields["numero_menage"] or "MENAGE_INCONNU"
        month_ref = parse_month_from_best_date(obj)
        best_dt = get_best_visit_datetime(obj)
        sort_dt = best_dt if best_dt is not None else datetime.max

        visits_by_household_month[(household_id, month_ref)].append((line_no, sort_dt))
        household_agents[household_id].add(fields["agent_id"])

visit_order_lookup = {}
household_month_count = {}

for (household_id, month_ref), rows in visits_by_household_month.items():
    rows_sorted = sorted(rows, key=lambda x: (x[1], x[0]))
    household_month_count[(household_id, month_ref)] = len(rows_sorted)
    for rank, (line_no, _sort_dt) in enumerate(rows_sorted, start=1):
        visit_order_lookup[line_no] = rank

# ============================================================
# PASS 2 - VISIT-LEVEL VALIDATION + AGGREGATION
# ============================================================

household_summary = defaultdict(Counter)
household_month_summary = defaultdict(Counter)

visit_rows = []

with open(SRC, "r", encoding="utf-8") as f:
    for line_no, line in enumerate(f, start=1):
        line = line.strip()
        if not line:
            continue

        obj = json.loads(line)
        fields = extract_register_fields(obj)

        household_id = fields["numero_menage"] or "MENAGE_INCONNU"
        household_name = fields["nom_chef_famille"] or ""
        agent_id = fields["agent_id"]
        visit_id = fields["visit_id"] or f"line_{line_no}"
        visit_dt = get_best_visit_datetime(obj)
        visit_dt_str = visit_dt.isoformat(sep=" ") if visit_dt else ""
        month_ref = parse_month_from_best_date(obj)
        visit_order_in_month = visit_order_lookup.get(line_no, 1)
        total_visits_this_month = household_month_count.get((household_id, month_ref), 1)

        raw_topics = extract_topics(obj)
        mapped_business_topics, unmapped_topics = map_topics_to_business(raw_topics)

        sujets_discutes_present = len(raw_topics) > 0
        has_allowed_theme = len(mapped_business_topics) > 0
        theme_hors_criteres = 1 if sujets_discutes_present and not has_allowed_theme else 0
        sujet_manquant = 1 if not sujets_discutes_present else 0

        missing_required = build_missing_required_fields(fields, sujets_discutes_present)
        missing_conditional = build_conditional_missing_fields(fields, visit_order_in_month)

        duration_minutes = compute_duration_minutes(fields["heure_debut"], fields["heure_fin"])
        duration_valid = duration_minutes is not None and 15 <= duration_minutes <= 30

        focus_group_normalized = normalize_yes_no(fields["focus_groupe"])
        focus_group_valid = focus_group_normalized in {"Oui", "Non"}

        # Strict visit conformity:
        # must satisfy ALL rules
        visit_is_conforme = (
            has_allowed_theme
            and len(missing_required) == 0
            and len(missing_conditional) == 0
            and duration_valid
            and focus_group_valid
        )

        visit_status = "CONFORME" if visit_is_conforme else "NON_CONFORME"

        # Household-month derived rule
        household_month_is_lt2 = total_visits_this_month < 2

        # Household-level counters
        hh = household_summary[household_id]
        hh["total_visites"] += 1
        hh["visites_conformes"] += 1 if visit_is_conforme else 0
        hh["visites_non_conformes"] += 1 if not visit_is_conforme else 0
        hh["visites_sans_sujet"] += sujet_manquant
        hh["visites_theme_hors_criteres"] += theme_hors_criteres
        hh["visites_sans_theme_autorise"] += 1 if not has_allowed_theme else 0
        hh["visites_avec_theme_autorise"] += 1 if has_allowed_theme else 0
        hh["visites_champs_obligatoires_manquants"] += 1 if len(missing_required) > 0 else 0
        hh["visites_revue_precedente_manquante"] += 1 if "revue_recommandations_precedente" in missing_conditional else 0
        hh["visites_duree_non_valide"] += 1 if not duration_valid else 0
        hh["visites_focus_groupe_non_renseigne"] += 1 if not focus_group_valid else 0
        hh["visites_nom_manquant"] += 1 if not nonempty(fields["nom_chef_famille"]) else 0
        hh["visites_personne_ciblee_manquante"] += 1 if not nonempty(fields["personnes_ciblees"]) else 0
        hh["agents_distincts"] = len(household_agents[household_id])

        # household-month summary
        hm = household_month_summary[(household_id, month_ref)]
        hm["total_visites"] += 1
        hm["visites_conformes"] += 1 if visit_is_conforme else 0
        hm["visites_non_conformes"] += 1 if not visit_is_conforme else 0

        # detailed visit row
        visit_rows.append({
            "visit_id": visit_id,
            "menage_id": household_id,
            "nom_chef_famille": household_name,
            "agent_id": agent_id,
            "date_visite_reference": visit_dt_str,
            "mois_reference": month_ref,
            "ordre_visite_dans_le_mois": visit_order_in_month,
            "nb_total_visites_menage_mois": total_visits_this_month,
            "sujets_bruts": " | ".join(raw_topics),
            "sujets_mappes_metier": " | ".join(mapped_business_topics),
            "sujets_non_mappes": " | ".join(unmapped_topics),
            "a_au_moins_un_theme_autorise": "Oui" if has_allowed_theme else "Non",
            "champs_obligatoires_manquants": " | ".join(missing_required),
            "champs_conditionnels_manquants": " | ".join(missing_conditional),
            "heure_debut": fields["heure_debut"] or "",
            "heure_fin": fields["heure_fin"] or "",
            "duree_minutes": duration_minutes if duration_minutes is not None else "",
            "duree_valide_15_30": "Oui" if duration_valid else "Non",
            "focus_groupe_normalise": focus_group_normalized or "",
            "focus_groupe_valide": "Oui" if focus_group_valid else "Non",
            "statut_conformite_visite": visit_status,
        })

# ============================================================
# PASS 3 - HOUSEHOLD-MONTH ROLLUP TO HOUSEHOLD
# ============================================================

household_month_rows = []

for (household_id, month_ref), stats in sorted(household_month_summary.items()):
    total_visits = stats["total_visites"]
    conformes = stats["visites_conformes"]
    non_conformes = stats["visites_non_conformes"]

    # Business rule:
    # "Chaque ménage doit être visité au moins 2 fois par mois pour être payé une seule fois."
    # Eligibility based on visit count is verifiable.
    eligible_payment_by_volume = "Oui" if total_visits >= 2 else "Non"

    # Actual "paid only once" is NOT verifiable without payment transaction data.
    payment_uniqueness_verifiable = "NON_VERIFIABLE"
    payment_theoretical_units = 1 if total_visits >= 2 else 0

    household_month_rows.append({
        "menage_id": household_id,
        "mois_reference": month_ref,
        "total_visites": total_visits,
        "visites_conformes": conformes,
        "visites_non_conformes": non_conformes,
        "eligible_paiement_selon_volume": eligible_payment_by_volume,
        "paiement_unique_verifiable": payment_uniqueness_verifiable,
        "paiement_theorique_unites": payment_theoretical_units,
    })

    hh = household_summary[household_id]
    hh["mois_total"] += 1
    if total_visits < 2:
        hh["mois_moins_de_2_visites"] += 1
        hh["visites_dans_mois_lt2"] += total_visits
    else:
        hh["mois_avec_visites_repetees"] += 1
        # From visit #2 onward, review of previous recommendations is required
        hh["visites_requerant_revue_precedente"] += (total_visits - 1)
        hh["mois_eligibles_paiement_selon_volume"] += 1
        hh["paiement_theorique_total"] += 1

# ============================================================
# EXPORT 1 - VISIT LEVEL VALIDATION
# ============================================================

with open(OUT_VISITS, "w", newline="", encoding="utf-8") as out:
    writer = csv.DictWriter(out, fieldnames=[
        "visit_id",
        "menage_id",
        "nom_chef_famille",
        "agent_id",
        "date_visite_reference",
        "mois_reference",
        "ordre_visite_dans_le_mois",
        "nb_total_visites_menage_mois",
        "sujets_bruts",
        "sujets_mappes_metier",
        "sujets_non_mappes",
        "a_au_moins_un_theme_autorise",
        "champs_obligatoires_manquants",
        "champs_conditionnels_manquants",
        "heure_debut",
        "heure_fin",
        "duree_minutes",
        "duree_valide_15_30",
        "focus_groupe_normalise",
        "focus_groupe_valide",
        "statut_conformite_visite",
    ])
    writer.writeheader()
    writer.writerows(visit_rows)

# ============================================================
# EXPORT 2 - HOUSEHOLD-MONTH VALIDATION
# ============================================================

with open(OUT_HH_MONTH, "w", newline="", encoding="utf-8") as out:
    writer = csv.DictWriter(out, fieldnames=[
        "menage_id",
        "mois_reference",
        "total_visites",
        "visites_conformes",
        "visites_non_conformes",
        "eligible_paiement_selon_volume",
        "paiement_unique_verifiable",
        "paiement_theorique_unites",
    ])
    writer.writeheader()
    writer.writerows(household_month_rows)

# ============================================================
# EXPORT 3 - HOUSEHOLD SUMMARY
# ============================================================

final_household_rows = []

for household_id, stats in household_summary.items():
    final_household_rows.append({
        "menage_id": household_id,
        "total_visites": stats["total_visites"],
        "visites_conformes": stats["visites_conformes"],
        "visites_non_conformes": stats["visites_non_conformes"],
        "mois_total": stats["mois_total"],
        "mois_moins_de_2_visites": stats["mois_moins_de_2_visites"],
        "visites_dans_mois_lt2": stats["visites_dans_mois_lt2"],
        "visites_sans_sujet": stats["visites_sans_sujet"],
        "visites_theme_hors_criteres": stats["visites_theme_hors_criteres"],
        "visites_sans_theme_autorise": stats["visites_sans_theme_autorise"],
        "visites_avec_theme_autorise": stats["visites_avec_theme_autorise"],
        "visites_champs_obligatoires_manquants": stats["visites_champs_obligatoires_manquants"],
        "visites_revue_precedente_manquante": stats["visites_revue_precedente_manquante"],
        "visites_requerant_revue_precedente": stats["visites_requerant_revue_precedente"],
        "visites_duree_non_valide": stats["visites_duree_non_valide"],
        "visites_focus_groupe_non_renseigne": stats["visites_focus_groupe_non_renseigne"],
        "visites_nom_manquant": stats["visites_nom_manquant"],
        "visites_personne_ciblee_manquante": stats["visites_personne_ciblee_manquante"],
        "mois_avec_visites_repetees": stats["mois_avec_visites_repetees"],
        "mois_eligibles_paiement_selon_volume": stats["mois_eligibles_paiement_selon_volume"],
        "paiement_theorique_total": stats["paiement_theorique_total"],
        "agents_distincts": stats["agents_distincts"],
        "paiement_unique_verifiable": "NON_VERIFIABLE",
        "principales_non_conformites": top_issues(stats),
    })

# Sort by severity:
# 1) off-criteria themes
# 2) missing subject
# 3) missing required fields
# 4) non-conforming visits
# 5) total visits
final_household_rows.sort(
    key=lambda r: (
        -r["visites_theme_hors_criteres"],
        -r["visites_sans_sujet"],
        -r["visites_champs_obligatoires_manquants"],
        -r["visites_non_conformes"],
        -r["total_visites"],
        r["menage_id"],
    )
)

with open(OUT_HH, "w", newline="", encoding="utf-8") as out:
    writer = csv.DictWriter(out, fieldnames=[
        "menage_id",
        "total_visites",
        "visites_conformes",
        "visites_non_conformes",
        "mois_total",
        "mois_moins_de_2_visites",
        "visites_dans_mois_lt2",
        "visites_sans_sujet",
        "visites_theme_hors_criteres",
        "visites_sans_theme_autorise",
        "visites_avec_theme_autorise",
        "visites_champs_obligatoires_manquants",
        "visites_revue_precedente_manquante",
        "visites_requerant_revue_precedente",
        "visites_duree_non_valide",
        "visites_focus_groupe_non_renseigne",
        "visites_nom_manquant",
        "visites_personne_ciblee_manquante",
        "mois_avec_visites_repetees",
        "mois_eligibles_paiement_selon_volume",
        "paiement_theorique_total",
        "agents_distincts",
        "paiement_unique_verifiable",
        "principales_non_conformites",
    ])
    writer.writeheader()
    writer.writerows(final_household_rows)

print(f"Generated: {Path(OUT_VISITS).resolve()}")
print(f"Generated: {Path(OUT_HH_MONTH).resolve()}")
print(f"Generated: {Path(OUT_HH).resolve()}")
