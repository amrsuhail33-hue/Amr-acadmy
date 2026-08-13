from flask import Flask, render_template, request

app = Flask(__name__)

# بناء هيكل البيانات برمجياً
DATA = {}
years = {"1": "السنة الأولى", "2": "السنة الثانية"}
terms = {"1": "الترم الأول", "2": "الترم الثاني"}

for y_id, y_name in years.items():
    DATA[y_id] = {"name": y_name, "terms": {}}
    
    for t_id, t_name in terms.items():
        subjects = []
        
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
            subj_names = [f"مادة {i}" for i in range(1, 8)]

        for i, s_name in enumerate(subj_names):
            # 10 مقاطع فيديو وشروحات لكل مادة
            videos = [{"title": f"فيديو / شرح {v}", "url": "#"} for v in range(1, 11)]
            
            # 10 ملفات لكل مادة
            explanations = [{"title": f"ملف {e}", "url": "#"} for e in range(1, 11)]
            
            subjects.append({
                "id": f"{y_id}_{t_id}_{i+1}",
                "name": s_name,
                "videos": videos,
                "explanations": explanations
            })
            
        DATA[y_id]["terms"][t_id] = {"name": t_name, "subjects": subjects}

# إدارة مسارات التنقل
@app.route('/')
def home():
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')  # 'videos' أو 'files'

    # 5. شاشة عرض المحتوى المحدد (إما مقاطع فيديو وشروحات أو ملفات)
    if subject_id and content_type:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='content', subject=s, content_type=content_type)

    # 4. شاشة اختيار نوع المحتوى
    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='select_type', subject=s)

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
