from flask import Flask, render_template, request

app = Flask(__name__)

# --- توليد هيكل البيانات برمجياً لاختصار الكود ---
DATA = {}
years = {"1": "السنة الأولى", "2": "السنة الثانية"}
terms = {"1": "الترم الأول", "2": "الترم الثاني"}

for y_id, y_name in years.items():
    DATA[y_id] = {"name": y_name, "terms": {}}
    
    for t_id, t_name in terms.items():
        subjects = []
        
        # تخصيص المواد للترم الحالي كمثال جاهز للاستخدام
        if y_id == "2" and t_id == "2":
            subj_names = [
                "آلات كهربائية (DC Motors)", 
                "إلكترونيات (Zener Diode)", 
                "الديناميكا الحرارية", 
                "ميكانيكا الموائع", 
                "مقاومة المواد", 
                "تطوير ويب (Flask)", 
                "تحكم كلاسيكي"
            ]
        else:
            # توليد 7 مواد افتراضية لباقي الأترام
            subj_names = [f"مادة {i}" for i in range(1, 8)]

        for i, s_name in enumerate(subj_names):
            # توليد فيديوهات (يمكنك مستقبلاً استبدال '#' بروابطك الحقيقية)
            videos = [{"title": f"فيديو شرح {v}", "url": "#"} for v in range(1, 6)]
            
            # توليد 20 ملف وشرح لكل مادة برمجياً
            explanations = [{"title": f"ملف / ملخص رقم {e}", "url": "#"} for e in range(1, 21)]
            
            subjects.append({
                "id": f"{y_id}_{t_id}_{i+1}",
                "name": s_name,
                "videos": videos,
                "explanations": explanations
            })
            
        DATA[y_id]["terms"][t_id] = {"name": t_name, "subjects": subjects}

# --- مسارات التنقل في الموقع ---
@app.route('/')
def home():
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')

    # 4. شاشة عرض المحتوى (فيديوهات وملفات)
    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='subject', subject=s)

    # 3. شاشة اختيار المادة
    if year and term:
        return render_template('index.html', view='subjects', 
                               year_name=DATA[year]["name"], 
                               term_name=DATA[year]["terms"][term]["name"], 
                               subjects=DATA[year]["terms"][term]["subjects"],
                               y_id=year)

    # 2. شاشة اختيار الترم
    if year:
        return render_template('index.html', view='terms', year_name=DATA[year]["name"], y_id=year)

    # 1. الشاشة الرئيسية (اختيار السنة)
    return render_template('index.html', view='years', data=DATA)

if __name__ == '__main__':
    app.run()
