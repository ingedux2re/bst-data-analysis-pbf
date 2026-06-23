import json, csv, re
from collections import Counter, defaultdict
from datetime import datetime

SRC = '/home/user/data/visit-a-domicile.json'
OUT_HH = '/mnt/user-data/outputs/nonconformites_par_menage.csv'
OUT_AGENT = '/mnt/user-data/outputs/nonconformites_par_agent.csv'
OUT_MONTH = '/mnt/user-data/outputs/nonconformites_par_mois.csv'
OUT_TEXT = '/mnt/user-data/outputs/resume_nonconformites_par_menage_agent_mois.txt'

# Mapping conservateur entre codes source et critères métier demandés
ALLOWED_THEME_MAP = {
    'anc_and_cpon_importance': 'suivi_nouveau_ne_pratiques_familiales',
    'child_vaccination': 'vaccination',
    'exclusive_breastfeeding': 'allaitement',
    'balanced_nutrition': 'alimentation_complement',
}

FIXED_STRUCTURAL_FIELDS = [
    'adresse', 'telephone', 'recommandations_faites',
    'prochain_rendez_vous', 'heure_debut', 'heure_fin', 'focus_groupe'
]


def nonempty(v):
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        return s != '' and s.lower() != 'nan'
    if isinstance(v, list):
        return len(v) > 0
    return True


def parse_month(s):
    if not nonempty(s):
        return None
    s = s.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f UTC', '%Y-%m-%d %H:%M:%S UTC', '%Y-%m-%d'):
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime('%Y-%m')
        except Exception:
            pass
    m = re.match(r'^(\d{4}-\d{2})', s)
    return m.group(1) if m else None


def get_themes(obj):
    arr = obj.get('covered_themes_array')
    if isinstance(arr, list) and arr:
        return [str(x).strip() for x in arr if str(x).strip()]
    s = obj.get('covered_themes')
    if isinstance(s, str) and s.strip():
        return [x.strip() for x in s.split() if x.strip()]
    return []


def agent_key(obj):
    submitter = (obj.get('submitter') or '').strip()
    if submitter:
        return submitter
    return (obj.get('reported_by') or '').strip() or 'AGENT_INCONNU'

# Pass 1: counts per household-month and base metadata
visits_by_hh_month = Counter()
agents_by_hh = defaultdict(set)
months_by_hh = defaultdict(set)

with open(SRC, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        hh = obj.get('family_uuid') or obj.get('visited_contact_uuid') or obj.get('inputs_contact__id') or 'MENAGE_INCONNU'
        month = parse_month(obj.get('visited_date')) or parse_month(obj.get('reported_date')) or 'MOIS_INCONNU'
        visits_by_hh_month[(hh, month)] += 1
        agents_by_hh[hh].add(agent_key(obj))
        months_by_hh[hh].add(month)

# Pass 2: aggregate non-conformities
hh_stats = defaultdict(Counter)
agent_stats = defaultdict(Counter)
month_stats = defaultdict(Counter)

for (hh, month), cnt in visits_by_hh_month.items():
    hh_stats[hh]['months_total'] += 1
    if cnt < 2:
        hh_stats[hh]['months_lt_2_visits'] += 1
        hh_stats[hh]['visits_in_lt_2_months'] += cnt
        month_stats[month]['households_lt_2_visits'] += 1
        month_stats[month]['visits_in_lt_2_household_months'] += cnt
    else:
        hh_stats[hh]['repeat_visit_months'] += 1
        # from 2nd visit onward, review_previous is required but absent in schema
        hh_stats[hh]['repeat_visits_missing_previous_review_est'] += (cnt - 1)
        month_stats[month]['repeat_visits_missing_previous_review_est'] += (cnt - 1)

with open(SRC, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        hh = obj.get('family_uuid') or obj.get('visited_contact_uuid') or obj.get('inputs_contact__id') or 'MENAGE_INCONNU'
        month = parse_month(obj.get('visited_date')) or parse_month(obj.get('reported_date')) or 'MOIS_INCONNU'
        agent = agent_key(obj)
        themes = get_themes(obj)
        any_allowed = any(t in ALLOWED_THEME_MAP for t in themes)
        subj_missing = 1 if len(themes) == 0 else 0
        theme_not_allowed = 1 if len(themes) > 0 and not any_allowed else 0
        name_missing = 0 if nonempty(obj.get('key_person_met_full_name') or obj.get('inputs_contact_name') or obj.get('muso_patient_name')) else 1
        person_missing = 0 if nonempty(obj.get('key_person_met_full_name') or obj.get('inputs_contact_name')) else 1
        cnt_month = visits_by_hh_month[(hh, month)]
        lt2 = 1 if cnt_month < 2 else 0

        targets = [hh_stats[hh], agent_stats[agent], month_stats[month]]
        for st in targets:
            st['total_visits'] += 1
            st['strict_nonconforming_visits'] += 1  # because required schema fields are absent
            st['missing_adresse'] += 1
            st['missing_telephone'] += 1
            st['missing_recommandations'] += 1
            st['missing_prochain_rendez_vous'] += 1
            st['missing_heure_debut'] += 1
            st['missing_heure_fin'] += 1
            st['missing_focus_groupe'] += 1
            st['subject_missing'] += subj_missing
            st['theme_not_allowed'] += theme_not_allowed
            st['name_missing'] += name_missing
            st['target_person_missing'] += person_missing
            st['visits_in_lt_2_household_months'] += lt2
            if any_allowed:
                st['visits_with_allowed_theme'] += 1
            else:
                st['visits_without_allowed_theme'] += 1

        hh_stats[hh]['agents_count'] = len(agents_by_hh[hh])
        hh_stats[hh]['household_id_present'] = 1
        agent_stats[agent]['households_served_set_count_placeholder'] += 0
        agent_stats[agent][f'household::{hh}'] += 0  # placeholder to track keys later if needed
        month_stats[month]['household_set_count_placeholder'] += 0
        month_stats[month][f'household::{hh}'] += 0
        agent_stats[agent][f'month::{month}'] += 0

# Derive unique counts for agent and month from placeholder keys
for agent, st in list(agent_stats.items()):
    hh_keys = [k for k in st.keys() if k.startswith('household::')]
    month_keys = [k for k in st.keys() if k.startswith('month::')]
    st['households_served'] = len(hh_keys)
    st['months_covered'] = len(month_keys)
    for k in hh_keys + month_keys + ['households_served_set_count_placeholder']:
        st.pop(k, None)

for month, st in list(month_stats.items()):
    hh_keys = [k for k in st.keys() if k.startswith('household::')]
    st['households_total'] = len(hh_keys)
    for k in hh_keys + ['household_set_count_placeholder']:
        st.pop(k, None)

# helper to define main non-conformities excluding universal structural gaps when looking for operational issues
SUMMARY_CATEGORIES = [
    ('theme_not_allowed', 'theme hors critères'),
    ('subject_missing', 'sujet discuté manquant'),
    ('visits_in_lt_2_household_months', 'moins de 2 visites dans le mois'),
    ('repeat_visits_missing_previous_review_est', 'revue précédente absente (estimation)'),
    ('name_missing', 'nom manquant'),
    ('target_person_missing', 'personne ciblée manquante'),
]

def top_issues(st, topn=3):
    vals = []
    for key, label in SUMMARY_CATEGORIES:
        v = st.get(key, 0)
        if v:
            vals.append((v, label))
    vals.sort(reverse=True)
    labels = [f'{label}: {v}' for v, label in vals[:topn]]
    return ' | '.join(labels) if labels else 'aucune hors lacunes structurelles'

# Write CSVs
with open(OUT_HH, 'w', newline='', encoding='utf-8') as out:
    w = csv.writer(out)
    w.writerow([
        'menage_id','total_visites','mois_total','mois_moins_de_2_visites','visites_dans_mois_lt2',
        'visites_sans_sujet','visites_theme_hors_criteres','visites_sans_theme_autorise',
        'visites_avec_theme_autorise','visites_nom_manquant','visites_personne_ciblee_manquante',
        'mois_avec_visites_repetees','visites_requérant_revue_precedente_absente_est','agents_distincts',
        'visites_strictement_non_conformes','principales_non_conformites'
    ])
    rows = []
    for hh, st in hh_stats.items():
        rows.append([
            hh, st['total_visits'], st['months_total'], st['months_lt_2_visits'], st['visits_in_lt_2_months'],
            st['subject_missing'], st['theme_not_allowed'], st['visits_without_allowed_theme'],
            st['visits_with_allowed_theme'], st['name_missing'], st['target_person_missing'],
            st['repeat_visit_months'], st['repeat_visits_missing_previous_review_est'], st['agents_count'],
            st['strict_nonconforming_visits'], top_issues(st)
        ])
    rows.sort(key=lambda r: (-r[6], -r[5], -r[4], -r[1], r[0]))
    w.writerows(rows)

with open(OUT_AGENT, 'w', newline='', encoding='utf-8') as out:
    w = csv.writer(out)
    w.writerow([
        'agent_id','total_visites','menages_servis','mois_couverts','visites_sans_sujet',
        'visites_theme_hors_criteres','visites_sans_theme_autorise','visites_avec_theme_autorise',
        'visites_dans_mois_lt2','visites_nom_manquant','visites_personne_ciblee_manquante',
        'visites_strictement_non_conformes','principales_non_conformites'
    ])
    rows = []
    for agent, st in agent_stats.items():
        rows.append([
            agent, st['total_visits'], st['households_served'], st['months_covered'], st['subject_missing'],
            st['theme_not_allowed'], st['visits_without_allowed_theme'], st['visits_with_allowed_theme'],
            st['visits_in_lt_2_household_months'], st['name_missing'], st['target_person_missing'],
            st['strict_nonconforming_visits'], top_issues(st)
        ])
    rows.sort(key=lambda r: (-r[5], -r[4], -r[8], -r[1], r[0]))
    w.writerows(rows)

with open(OUT_MONTH, 'w', newline='', encoding='utf-8') as out:
    w = csv.writer(out)
    w.writerow([
        'mois','total_visites','menages_uniques','menages_moins_de_2_visites','visites_dans_mois_lt2',
        'visites_sans_sujet','visites_theme_hors_criteres','visites_sans_theme_autorise','visites_avec_theme_autorise',
        'visites_nom_manquant','visites_personne_ciblee_manquante','visites_requérant_revue_precedente_absente_est',
        'visites_strictement_non_conformes','principales_non_conformites'
    ])
    rows = []
    for month, st in month_stats.items():
        rows.append([
            month, st['total_visits'], st['households_total'], st['households_lt_2_visits'], st['visits_in_lt_2_household_months'],
            st['subject_missing'], st['theme_not_allowed'], st['visits_without_allowed_theme'], st['visits_with_allowed_theme'],
            st['name_missing'], st['target_person_missing'], st['repeat_visits_missing_previous_review_est'],
            st['strict_nonconforming_visits'], top_issues(st)
        ])
    rows.sort(key=lambda r: r[0])
    w.writerows(rows)

# Build text summary with top examples
# Top household by operational non-conformities (excluding universal structural gaps)
def rank_household(item):
    hh, st = item
    return (st['theme_not_allowed'] + st['subject_missing'] + st['visits_in_lt_2_months'] + st['repeat_visits_missing_previous_review_est'], st['theme_not_allowed'], st['subject_missing'], st['visits_in_lt_2_months'], st['total_visits'])

def rank_agent(item):
    ag, st = item
    return (st['theme_not_allowed'] + st['subject_missing'] + st['visits_in_lt_2_household_months'], st['theme_not_allowed'], st['subject_missing'], st['visits_in_lt_2_household_months'], st['total_visits'])

hh_top = sorted(hh_stats.items(), key=rank_household, reverse=True)[:10]
agent_top = sorted(agent_stats.items(), key=rank_agent, reverse=True)[:10]
month_rows = sorted(month_stats.items(), key=lambda x: x[0])

lines = []
lines.append('RESUME DES PRINCIPALES NON-CONFORMITES')
lines.append('Perimetre: /vad/visit-a-domicile.json')
lines.append('Definition: le fichier presente des lacunes structurelles universelles (adresse, telephone, recommandations, prochain rendez-vous, heure debut, heure fin, focus groupe).')
lines.append('Pour eviter un classement peu utile, les regroupements ci-dessous mettent l accent sur les non-conformites operationnelles: sujet manquant, theme hors criteres, et menage avec moins de 2 visites dans le mois.')
lines.append('')
lines.append('1) Par mois')
for month, st in month_rows:
    lines.append(f"- {month}: visites={st['total_visits']}; theme_hors_criteres={st['theme_not_allowed']}; sujet_manquant={st['subject_missing']}; menages_lt2={st['households_lt_2_visits']}; visites_revue_precedente_absente_est={st['repeat_visits_missing_previous_review_est']}")
lines.append('')
lines.append('2) Top 10 menages avec le plus de non-conformites operationnelles')
for hh, st in hh_top:
    lines.append(f"- {hh}: visites={st['total_visits']}; theme_hors_criteres={st['theme_not_allowed']}; sujet_manquant={st['subject_missing']}; visites_dans_mois_lt2={st['visits_in_lt_2_months']}; revue_precedente_absente_est={st['repeat_visits_missing_previous_review_est']}; principales={top_issues(st)}")
lines.append('')
lines.append('3) Top 10 agents avec le plus de non-conformites operationnelles')
for ag, st in agent_top:
    lines.append(f"- {ag}: visites={st['total_visits']}; theme_hors_criteres={st['theme_not_allowed']}; sujet_manquant={st['subject_missing']}; visites_dans_mois_lt2={st['visits_in_lt_2_household_months']}; principales={top_issues(st)}")
lines.append('')
lines.append('4) Lecture')
lines.append('- Les non-conformites structurelles touchent 100% des visites parce que ces champs ne sont pas dans le JSON source.')
lines.append('- Les differences entre menages, agents et mois viennent surtout des themes saisis hors de la liste de criteres, de l absence de sujet saisi, et des menages avec moins de 2 visites sur le mois.')
lines.append('- L indicateur revue_precedente_absente_est est une estimation mecanique: il compte les visites a partir de la 2e visite mensuelle, car le champ de revue precedente est absent du schema.')

with open(OUT_TEXT, 'w', encoding='utf-8') as out:
    out.write('\n'.join(lines))

print(OUT_HH)
print(OUT_AGENT)
print(OUT_MONTH)
print(OUT_TEXT)
