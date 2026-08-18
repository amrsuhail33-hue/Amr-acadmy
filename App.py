import os
import re
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, session
)
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# مفتاح الجلسات (Session Secret)
app.secret_key = os.environ.get('SECRET_KEY', 'default-engineering-secret-key-2026')

# --- ضبط رابط قاعدة البيانات والتوافق مع Supabase PostgreSQL ---
raw_database_url = os.environ.get('DATABASE_URL')

if raw_database_url:
    # إصلاح بادئة الرابط لتتوافق مع SQLAlchemy و Psycopg2
    if raw_database_url.startswith("postgres://"):
        raw_database_url = raw_database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif raw_database_url.startswith("postgresql://") and not raw_database_url.startswith("postgresql+psycopg2://"):
        raw_database_url = raw_database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    
    app.config['SQLALCHEMY_DATABASE_URI'] = raw_database_url
else:
    # استخدام قاعدة بيانات محلية كاحتياط في حال عدم وجود متغير البيئة
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///local_backup.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)

# كلمة مرور لوحة الإدارة
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

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

# إنشاء الجداول تلقائياً إن لم تكن منشأة
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Database Init Note: {e}")

# --- حماية المسارات (Admin Decorator) ---

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            flash('يرجى تسجيل الدخول كمسؤول للوصول إلى هذه الصفحة.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# --- المسارات والوظائف (Routes) ---

@app.route('/')
def index():
    updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    return render_template('index.html', updates=updates)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            flash('تم تسجيل الدخول بنجاح!', 'success')
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
    questions = LessonQuestion.query.order_by(LessonQuestion.created_at.desc()).all()
    lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
    return render_template('admin.html', updates=updates, questions=questions, lessons=lessons)

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_update():
    title = request.form.get('title')
    tag = request.form.get('tag', 'ميزة جديدة')
    description = request.form.get('description')
    
    if title and description:
        new_update = PlatformUpdate(title=title, tag=tag, description=description)
        db.session.add(new_update)
        db.session.commit()
        flash('تم نشر التحديث بنجاح!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete-update/<int:id>', methods=['POST'])
@admin_required
def delete_update(id):
    update = PlatformUpdate.query.get_or_404(id)
    db.session.delete(update)
    db.session.commit()
    flash('تم حذف التحديث.', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/ask-question', methods=['POST'])
def ask_question():
    subject_id = request.form.get('subject_id')
    chapter_name = request.form.get('chapter_name')
    student_name = request.form.get('student_name', 'طالب')
    question_text = request.form.get('question_text')
    
    if subject_id and chapter_name and question_text:
        question = LessonQuestion(
            subject_id=subject_id,
            chapter_name=chapter_name,
            student_name=student_name,
            question_text=question_text
        )
        db.session.add(question)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'تم إرسال سؤالك وسيتم الرد عليه قريباً!'})
    return jsonify({'status': 'error', 'message': 'بيانات ناقصة'}), 400

@app.route('/admin/answer-question/<int:id>', methods=['POST'])
@admin_required
def answer_question(id):
    question = LessonQuestion.query.get_or_404(id)
    answer_text = request.form.get('answer_text')
    
    if answer_text:
        question.answer_text = answer_text
        question.is_answered = True
        db.session.commit()
        flash('تم حفظ ونشر الإجابة للطالب!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add-lesson', methods=['POST'])
@admin_required
def add_lesson():
    year = request.form.get('year')
    term = request.form.get('term')
    subject_name = request.form.get('subject_name')
    chapter_name = request.form.get('chapter_name')
    content_type = request.form.get('content_type')
    title = request.form.get('title')
    url = request.form.get('url')
    download_url = request.form.get('download_url')
    notes = request.form.get('notes')

    new_lesson = CustomLesson(
        year=year,
        term=term,
        subject_name=subject_name,
        chapter_name=chapter_name,
        content_type=content_type,
        title=title,
        url=url,
        download_url=download_url,
        notes=notes
    )
    db.session.add(new_lesson)
    db.session.commit()
    flash('تم إضافة الدرس بنجاح!', 'success')
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
