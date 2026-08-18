import os
import re
import requests
import pandas as pd
from io import StringIO
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, session
)
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# مفتاح الجلسات
app.secret_key = os.environ.get('SECRET_KEY', 'amr-academy-secret-key-2026')

# --- ضبط رابط قاعدة البيانات والتوافق مع Supabase PostgreSQL ---
raw_database_url = os.environ.get('DATABASE_URL')

if raw_database_url:
    if raw_database_url.startswith("postgres://"):
        raw_database_url = raw_database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif raw_database_url.startswith("postgresql://") and not raw_database_url.startswith("postgresql+psycopg2://"):
        raw_database_url = raw_database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = raw_database_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///local_backup.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)

# المتغيرات السرية
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# رابط تصدير Google Sheets بصيغة CSV
GOOGLE_SHEET_CSV_URL = os.environ.get(
    'GOOGLE_SHEET_CSV_URL',
    'https://docs.google.com/spreadsheets/d/e/2PACX-1vReL7kXN44-WnI9wX4Wf1G0kQ_your_sheet_id/pub?output=csv'
)

# --- نماذج قاعدة البيانات (Database Models) ---

class CustomLesson(db.Model):
    __tablename__ = 'custom_lessons'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    content_type = db.Column(db.String(20), nullable=False)  # 'video' أو 'pdf'
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=True)
    download_url = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LessonQuestion(db.Model):
    __tablename__ = 'lesson_questions'
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    student_name = db.Column(db.String(100), default='طالب')
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text, nullable=True)
    is_answered = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default='ميزة جديدة')
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Database Init Error: {e}")

# --- دوال المساعدة وجلب البيانات ---

def fetch_sheet_lessons():
    """جلب وتنسيق الدروس من Google Sheets مباشرة"""
    try:
        if 'docs.google.com' not in GOOGLE_SHEET_CSV_URL:
            return []
        response = requests.get(GOOGLE_SHEET_CSV_URL, timeout=8)
        if response.status_code == 200:
            csv_data = StringIO(response.text)
            df = pd.read_csv(csv_data)
            df = df.fillna('')
            return df.to_dict(orient='records')
    except Exception as err:
        print(f"Sheet Fetch Error: {err}")
    return []

def get_all_combined_lessons():
    """دمج دروس Google Sheets مع الدروس المضافة في قاعدة البيانات"""
    sheet_data = fetch_sheet_lessons()
    db_lessons = CustomLesson.query.all()
    
    combined = []
    for item in sheet_data:
        combined.append({
            'source': 'sheet',
            'id': None,
            'year': str(item.get('year', item.get('السنة', ''))).strip(),
            'term': str(item.get('term', item.get('الترم', ''))).strip(),
            'subject_name': str(item.get('subject_name', item.get('المادة', ''))).strip(),
            'chapter_name': str(item.get('chapter_name', item.get('الفصل', ''))).strip(),
            'content_type': str(item.get('content_type', item.get('النوع', 'video'))).strip(),
            'title': str(item.get('title', item.get('العنوان', ''))).strip(),
            'url': str(item.get('url', item.get('الرابط', ''))).strip(),
            'download_url': str(item.get('download_url', item.get('رابط_التحميل', ''))).strip(),
            'notes': str(item.get('notes', item.get('ملاحظات', ''))).strip(),
        })

    for d in db_lessons:
        combined.append({
            'source': 'db',
            'id': d.id,
            'year': d.year,
            'term': d.term,
            'subject_name': d.subject_name,
            'chapter_name': d.chapter_name,
            'content_type': d.content_type,
            'title': d.title,
            'url': d.url,
            'download_url': d.download_url,
            'notes': d.notes,
        })
    return combined

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            flash('يرجى تسجيل الدخول كمسؤول للوصول إلى هذه الصفحة.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# --- المسارات الرئيسية للطلاب ---

@app.route('/')
def index():
    updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).limit(5).all()
    all_lessons = get_all_combined_lessons()
    
    years = sorted(list(set(item['year'] for item in all_lessons if item['year'])))
    subjects = sorted(list(set(item['subject_name'] for item in all_lessons if item['subject_name'])))
    
    return render_template(
        'index.html',
        updates=updates,
        years=years,
        subjects=subjects,
        total_lessons=len(all_lessons)
    )

@app.route('/subject/<subject_name>')
def view_subject(subject_name):
    all_lessons = get_all_combined_lessons()
    subject_lessons = [l for l in all_lessons if l['subject_name'] == subject_name]
    
    # جلب الأسئلة والمناقشات المجاب عليها لهذه المادة
    answered_questions = LessonQuestion.query.filter_by(
        subject_id=subject_name,
        is_answered=True
    ).order_by(LessonQuestion.created_at.desc()).all()
    
    return render_template(
        'subject.html',
        subject_name=subject_name,
        lessons=subject_lessons,
        questions=answered_questions
    )

@app.route('/ask-question', methods=['POST'])
def ask_question():
    subject_id = request.form.get('subject_id')
    chapter_name = request.form.get('chapter_name', 'عام')
    student_name = request.form.get('student_name', 'طالب')
    question_text = request.form.get('question_text')
    
    if subject_id and question_text:
        new_q = LessonQuestion(
            subject_id=subject_id,
            chapter_name=chapter_name,
            student_name=student_name,
            question_text=question_text
        )
        db.session.add(new_q)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'تم إرسال استفسارك وسيتم الرد عليه قريباً!'})
    return jsonify({'status': 'error', 'message': 'يرجى كتابة نص السؤال'}), 400

# --- حاسبة المعدل التراكمي ومولد الخطط بالذكاء الاصطناعي ---

@app.route('/gpa-calculator', methods=['GET', 'POST'])
def gpa_calculator():
    result = None
    if request.method == 'POST':
        try:
            prev_gpa = float(request.form.get('prev_gpa', 0))
            prev_hours = float(request.form.get('prev_hours', 0))
            current_grades = request.form.getlist('grade[]')
            current_hours = request.form.getlist('hours[]')
            
            total_current_points = 0
            total_current_hours = 0
            
            for g, h in zip(current_grades, current_hours):
                if g and h:
                    total_current_points += float(g) * float(h)
                    total_current_hours += float(h)
                    
            total_points = (prev_gpa * prev_hours) + total_current_points
            total_all_hours = prev_hours + total_current_hours
            
            cumulative_gpa = (total_points / total_all_hours) if total_all_hours > 0 else 0
            term_gpa = (total_current_points / total_current_hours) if total_current_hours > 0 else 0
            
            result = {
                'cumulative_gpa': round(cumulative_gpa, 2),
                'term_gpa': round(term_gpa, 2),
                'total_hours': total_all_hours
            }
        except Exception as e:
            flash('حدث خطأ أثناء حساب المعدل، تأكد من صحة الأرقام المدخلة.', 'danger')
            
    return render_template('gpa_calculator.html', result=result)

@app.route('/generate-study-plan', methods=['POST'])
def generate_study_plan():
    if not GROQ_API_KEY:
        return jsonify({'status': 'error', 'message': 'مفتاح الذكاء الاصطناعي غير متوفر حالياً'}), 500
        
    data = request.get_json() or {}
    subjects_info = data.get('subjects', '')
    daily_hours = data.get('daily_hours', '4')
    target_goal = data.get('goal', 'الامتياز والتركيز على الجانب العملي والهندسي')
    
    prompt = f"""
    أنت مستشار أكاديمي وهندسي خبير. قم بإنشاء خطة مذاكرة وتوزيع دراسي منظم أسبوعياً لطالب هندسة ميكاترونكس بناءً على المدخلات التالية:
    - المواد الدراسية: {subjects_info}
    - عدد ساعات المذاكرة اليومية المتاحة: {daily_hours} ساعات
    - الهدف الدراسي: {target_goal}
    
    نسّق الإجابة بنقاط واضحة ومباشرة بالعربية تشمل جدولاً أسبوعياً ونصائح للاختبارات.
    """
    
    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6
        }
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=25)
        if res.status_code == 200:
            plan_text = res.json()['choices'][0]['message']['content']
            return jsonify({'status': 'success', 'plan': plan_text})
        else:
            return jsonify({'status': 'error', 'message': 'تعذر استخراج الخطة من السيرفر'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- لوحة التحكم والإدارة (Admin Dashboard) ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            flash('تم تسجيل الدخول إلى لوحة التحكم بنجاح!', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('كلمة المرور غير صحيحة!', 'danger')
    return render_template('login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    flash('تم تسجيل الخروج بنجاح.', 'info')
    return redirect(url_for('index'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    questions = LessonQuestion.query.order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
    lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
    return render_template('admin.html', updates=updates, questions=questions, lessons=lessons)

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_update():
    title = request.form.get('title')
    tag = request.form.get('tag', 'ميزة جديدة')
    description = request.form.get('description')
    
    if title and description:
        new_up = PlatformUpdate(title=title, tag=tag, description=description)
        db.session.add(new_up)
        db.session.commit()
        flash('تم نشر التحديث بنجاح!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete-update/<int:id>', methods=['POST'])
@admin_required
def delete_update(id):
    item = PlatformUpdate.query.get_or_404(id)
    db.session.delete(item)
    db.session.commit()
    flash('تم حذف التحديث.', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/answer-question/<int:id>', methods=['POST'])
@admin_required
def answer_question(id):
    question = LessonQuestion.query.get_or_404(id)
    answer_text = request.form.get('answer_text')
    
    if answer_text:
        question.answer_text = answer_text
        question.is_answered = True
        db.session.commit()
        flash('تم نشر الرد على السؤال بنجاح!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete-question/<int:id>', methods=['POST'])
@admin_required
def delete_question(id):
    question = LessonQuestion.query.get_or_404(id)
    db.session.delete(question)
    db.session.commit()
    flash('تم حذف السؤال.', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add-lesson', methods=['POST'])
@admin_required
def add_lesson():
    new_lesson = CustomLesson(
        year=request.form.get('year', ''),
        term=request.form.get('term', ''),
        subject_name=request.form.get('subject_name', ''),
        chapter_name=request.form.get('chapter_name', ''),
        content_type=request.form.get('content_type', 'video'),
        title=request.form.get('title', ''),
        url=request.form.get('url', ''),
        download_url=request.form.get('download_url', ''),
        notes=request.form.get('notes', '')
    )
    db.session.add(new_lesson)
    db.session.commit()
    flash('تم حفظ الدرس في قاعدة البيانات بنجاح!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete-lesson/<int:id>', methods=['POST'])
@admin_required
def delete_lesson(id):
    lesson = CustomLesson.query.get_or_404(id)
    db.session.delete(lesson)
    db.session.commit()
    flash('تم حذف الدرس بنجاح.', 'info')
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
