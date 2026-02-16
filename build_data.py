#!/usr/bin/env python3
"""
build_data.py – Converts dataset.xlsx → data.json
Run this once before deploying. The HTML dashboard reads data.json directly.
"""
import pandas as pd
import json
import os
import sys

EXCEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.xlsx")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

def main():
    if not os.path.exists(EXCEL_PATH):
        print(f"ERROR: {EXCEL_PATH} not found. Place dataset.xlsx next to this script.")
        sys.exit(1)

    df = pd.read_excel(EXCEL_PATH)
    print(f"Loaded {len(df)} rows from dataset.xlsx")

    # Normalize Likert casing
    likert_map = {
        'muy satisfecho': 'Muy satisfecho',
        'satisfecho': 'Satisfecho',
        'neutral': 'Neutral',
        'insatisfecho': 'Insatisfecho',
        'muy insatisfecho': 'Muy insatisfecho',
    }

    skip_keywords = ['marca temporal', 'nombre del alumno', 'campus', 'nivel educativo',
                     'grado', '¿por qué', 'comentarios', 'sugerencias']

    question_cols = []
    for c in df.columns:
        if c is None or str(c) == 'None':
            continue
        if any(kw in str(c).lower() for kw in skip_keywords):
            continue
        vals = df[c].dropna().unique()
        if len(vals) <= 10:
            question_cols.append(c)

    likert5_cols = []
    yesno_cols = []

    for c in question_cols:
        vals = set(str(v).strip().lower() for v in df[c].dropna().unique())
        if vals <= {'muy satisfecho', 'satisfecho', 'neutral', 'insatisfecho', 'muy insatisfecho'}:
            likert5_cols.append(c)
            df[c] = df[c].apply(lambda x: likert_map.get(str(x).strip().lower(), x) if pd.notna(x) else x)
        elif vals <= {'sí', 'si', 'no'}:
            yesno_cols.append(c)

    df['plantel'] = df['Nivel Educativo'].str.strip() + ' \u2013 ' + df['Campus'].str.strip()

    LIKERT5_ORDER = ['Muy satisfecho', 'Satisfecho', 'Neutral', 'Insatisfecho', 'Muy insatisfecho']
    YESNO_ORDER = ['Sí', 'No']
    all_question_cols = likert5_cols + yesno_cols
    planteles = sorted(df['plantel'].dropna().unique().tolist())

    def compute_plantel(sub, label):
        total = len(sub)
        results = []
        for col in all_question_cols:
            is_likert5 = col in likert5_cols
            order = LIKERT5_ORDER if is_likert5 else YESNO_ORDER
            counts = sub[col].value_counts()
            data = []
            for lbl in order:
                c = int(counts.get(lbl, 0))
                data.append({'label': lbl, 'count': c, 'pct': round(c / total * 100, 1) if total > 0 else 0})
            results.append({
                'question': col,
                'type': 'likert5' if is_likert5 else 'yesno',
                'total': total,
                'data': data,
            })
        return {'name': label, 'total': total, 'questions': results}

    output = {'planteles': {}}
    output['planteles']['__ALL__'] = compute_plantel(df, 'Todos los Planteles')
    for p in planteles:
        output['planteles'][p] = compute_plantel(df[df['plantel'] == p], p)

    output['plantel_list'] = ['__ALL__'] + planteles

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"  Planteles: {len(planteles)} + global")
    print(f"  Likert-5 questions: {len(likert5_cols)}")
    print(f"  Sí/No questions: {len(yesno_cols)}")
    print("Done! Now deploy index.html + data.json to Vercel.")

if __name__ == '__main__':
    main()
