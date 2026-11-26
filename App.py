import os
import io
import csv
import re
import time
import json
import ssl
import base64
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, send_file, redirect, url_for, session,
    make_response
)
from urllib.parse import quote as _url_quote, unquote as _url_unquote
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Uuid, func
from sqlalchemy.sql import text
from groq import Groq
from werkzeug.security import generate_password_hash, check_password_hash
import math

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)

# ==========================================
# Database Setup
# ==========================================
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()

if raw_db_url.startswith("postgres://"):
    db_url = raw_db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif raw_db_url.startswith("postgresql://"):
    db_url = raw_db_url.replace("postgresql://", "postgresql+pg8000://", 1)
else:
    db_url = raw_db_url

db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")

if "?" in db_url:
    db_url = db_url.split("?")[0]

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if "sqlite" not in db_url:
    # Render Postgres يستخدم شهادة موقعة ذاتياً، فالتحقق الافتراضي يفشل
    # (SSLCertVerificationError, tls_verify_code=19). إذا وفّرنا ملف CA عبر
    # PGSSLROOTCERT نبقي التحقق مفعّلاً، وإلا نعطّل التحقق لهذا الاتصال فقط.
    ssl_context = ssl.create_default_context()
    ca_file = os.environ.get("PGSSLROOTCERT")
    if ca_file:
        ssl_context.load_verify_locations(cafile=ca_file)
    else:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {
            "ssl_context": ssl_context
        }
    }


db = SQLAlchemy(app)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
GROQ_MODEL = "openai/gpt-oss-120b"

# رسالة عامة واحدة لكل فشل في خدمات الذكاء الاصطناعي (بوت / توليد الخطط /
# إعادة التوزيع). لا يُعرض أي نص خطأ خام للمستخدم أبداً — لا رمز حالة،
# لا رسالة استثناء، ولا أي ذكر لمفتاح API؛ التفاصيل تُسجَّل في سجل الخادم فقط.
AI_UNAVAILABLE_MSG = "عذراً، الخدمة غير متاحة حالياً، حاول لاحقاً"

# ==========================================
# Database Models
# ==========================================

class Student(db.Model):
    __tablename__ = 'students'
    __table_args__ = (
        CheckConstraint(
            "role IN ('student', 'assistant', 'admin', 'college_supervisor', 'specialization_supervisor', 'store_manager')",
            name='students_role_check',
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    # نوع Uuid الأصلي: يظهر كـ UUID في Postgres (مطابق للعمود الموجود فلا حاجة لـ migration) وكـ CHAR في SQLite
    auth_user_id = db.Column(Uuid, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(50), default='student')  # student, assistant, admin, college_supervisor, specialization_supervisor, store_manager (الأطول 25 حرفاً — كان String(20) فيفشل حفظه)
    college = db.Column(db.String(150), nullable=True)
    supervisor_college = db.Column(db.String(150), nullable=True)  # نطاق المشرف: اسم الكلية التي يشرف عليها
    supervisor_specialization = db.Column(db.String(150), nullable=True)  # نطاق مشرف التخصص: اسم المادة
    supervisor_subject_id = db.Column(db.Integer, nullable=True)  # نطاق مشرف التخصص الدقيق: معرّف المادة (Q4)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    profile = db.relationship('UserProfile', backref='student', uselist=False, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        try:
            return check_password_hash(self.password_hash, password)
        except Exception:
            return self.password_hash == password

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "role": self.role or 'student',
            "college": self.college or '',
            "supervisor_college": self.supervisor_college or '',
            "supervisor_specialization": self.supervisor_specialization or '',
            "supervisor_subject_id": self.supervisor_subject_id,
            "is_active": bool(self.is_active) if self.is_active is not None else True,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class College(db.Model):
    __tablename__ = 'colleges'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class Subject(db.Model):
    """المادة ككيان مستقل: الاسم + الكلية + السنة + الترم.
    قبل هذا كانت «المادة» مجرد نص subject_name على كل درس/سؤال، فتعديل
    اسمها كان يعدّل صفاً واحداً فقط وتظهر مادة مكررة. الآن إعادة التسمية
    تحدّث الكيان وكل صفوفه المرتبطة دفعة واحدة."""
    __tablename__ = 'subjects'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    college = db.Column(db.String(150), nullable=False)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('college', 'year', 'term', 'name',
                                           name='uq_subject_college_year_term_name'),)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "college": self.college,
            # عرض فقط: سنة مخزنة بإملاء خاطئ («السنة الاولئ») تُعرض صحيحة
            # دون تغيير الصف المخزن حتى لا تنكسر روابط الدروس/الأسئلة.
            "year": _display_year(self.year),
            "term": self.term,
            "lesson_count": _subject_lesson_count(self),
            "question_count": _subject_question_count(self),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class DoctorOpinion(db.Model):
    """رأي طالب عن دكتور (دليل الطالب): لا يظهر للطلاب إلا بعد موافقة
    المشرف الأعلى. المعلق قد يخفي اسمه فيظهر «مجهول» بدل اسمه."""
    __tablename__ = 'doctor_opinions'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, nullable=False)
    doctor_name = db.Column(db.String(150), nullable=False)
    subject = db.Column(db.String(150), nullable=False)
    college = db.Column(db.String(150), nullable=False)
    comment = db.Column(db.Text, nullable=False)
    anonymous = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default='pending')  # pending / approved / rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, viewer_id=None, include_real_author=False):
        likes = DoctorOpinionVote.query.filter_by(opinion_id=self.id, value=1).count()
        dislikes = DoctorOpinionVote.query.filter_by(opinion_id=self.id, value=-1).count()
        user_vote = 0
        if viewer_id:
            v = DoctorOpinionVote.query.filter_by(opinion_id=self.id, student_id=viewer_id).first()
            user_vote = v.value if v else 0
        author = None
        if not self.anonymous:
            s = Student.query.get(self.student_id)
            author = s.name if s else None
        result = {
            "id": self.id,
            "doctor_name": self.doctor_name,
            "subject": self.subject,
            "college": self.college,
            "comment": self.comment,
            "author_name": author or "مجهول",
            "anonymous": bool(self.anonymous),
            "status": self.status,
            "likes": likes,
            "dislikes": dislikes,
            "user_vote": user_vote,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }
        if include_real_author:
            s = Student.query.get(self.student_id)
            result["student_name"] = s.name if s else ""
            result["student_username"] = s.username if s else ""
        return result

class DoctorOpinionVote(db.Model):
    """صوت طالب على رأي: +1 لايك / -1 ديسلايك — صوت واحد لكل طالب على كل رأي."""
    __tablename__ = 'doctor_opinion_votes'
    id = db.Column(db.Integer, primary_key=True)
    opinion_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    value = db.Column(db.Integer, nullable=False)  # 1 = لايك، -1 = ديسلايك
    __table_args__ = (db.UniqueConstraint('opinion_id', 'student_id',
                                           name='uq_opinion_vote'),)

class Store(db.Model):
    """متجر داخل «خدمات»: اسم + وصف + رقم واتساب + مدير + حالة + صورة.
    المتجر الموقوف (paused) لا يظهر للمتسوقين. مدير المتجر دوره store_manager."""
    __tablename__ = 'stores'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    image_url = db.Column(db.String(500), nullable=True)  # رابط صورة المتجر (اختياري)
    whatsapp_number = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), default='active')  # active / paused
    manager_id = db.Column(db.Integer, nullable=True)
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        manager = Student.query.get(self.manager_id) if self.manager_id else None
        product_count = Product.query.filter_by(store_id=self.id).count()
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "image_url": self.image_url or "",
            "whatsapp_number": self.whatsapp_number,
            "status": self.status or 'active',
            "manager_id": self.manager_id,
            "manager_name": manager.name if manager else None,
            "manager_username": manager.username if manager else None,
            "product_count": product_count,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class Product(db.Model):
    """منتج في متجر: اسم + سعر (عدد صحيح) + عملة (YER/SAR/USD، الافتراضي YER)
    + رابط صورة + تفاصيل اختيارية."""
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), nullable=True, default='YER')
    image_url = db.Column(db.String(500), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "store_id": self.store_id,
            "name": self.name,
            "price": self.price,
            "currency": self.currency or 'YER',
            "image_url": self.image_url or "",
            "details": self.details or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class VipGrant(db.Model):
    """منح VIP: وصول مدفوع لطالب إلى هدف محدد (مادة/محتوى/خدمة) حتى تاريخ
    انتهاء، أو أبدي عند expires_at NULL. كل شيء مجاني افتراضياً — لا يغلق
    أي وصول حتى يمنح الأدمن منحاً ويعلّم محتوى/خدمة كـ VIP."""
    __tablename__ = 'vip_grants'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, nullable=False)
    scope_type = db.Column(db.String(20), nullable=False)  # 'subject' | 'content' | 'service'
    scope_key = db.Column(db.String(150), nullable=False)  # معرّف المادة أو الدرس أو مفتاح الخدمة
    expires_at = db.Column(db.DateTime, nullable=True)  # NULL = أبدي
    granted_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "expires_at": self.expires_at.strftime("%Y-%m-%d %H:%M") if self.expires_at else None,
            "is_forever": self.expires_at is None,
            "granted_by": self.granted_by,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class VipService(db.Model):
    """مفتاح إغلاق خدمة من خدمات الفقاعة كـ VIP: الصف موجود والعمود True
    يعني الخدمة مغلقة لمن ليس لديه منح خدمة عليها. الافتراضي: الكل مجاني."""
    __tablename__ = 'vip_services'
    service_key = db.Column(db.String(50), primary_key=True)
    is_vip = db.Column(db.Boolean, default=False)

class DeletedAccount(db.Model):
    """شاهد قبر لحساب محذوف: يحتفظ باليوزر حتى تعرض محاولة دخول لاحقة
    رسالة الحذف بدل رسالة دخول عامة. لا يحفظ أي كلمة سر."""
    __tablename__ = 'deleted_accounts'
    username = db.Column(db.String(80), primary_key=True)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reason = db.Column(db.String(200), nullable=True)

class UserProfile(db.Model):
    __tablename__ = 'user_profiles'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), unique=True, nullable=False)
    level = db.Column(db.Integer, default=1)
    points = db.Column(db.Integer, default=0)
    total_points = db.Column(db.Integer, default=0)
    show_in_leaderboard = db.Column(db.Boolean, default=True)
    streak_days = db.Column(db.Integer, default=0)
    last_active = db.Column(db.Date, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def calculate_level(self):
        if self.points < 0:
            self.points = 0
        new_level = math.floor(self.points / 100) + 1
        return max(new_level, 1)

    def add_points(self, points, reason="نشاط عام"):
        if points <= 0:
            return {"level_up": False, "new_level": self.level, "new_points": self.points}

        recent = PointTransaction.query.filter_by(student_id=self.student_id, reason=reason).first()
        if recent and "محاولة اختبار" not in reason:
            return {"level_up": False, "new_level": self.level, "new_points": self.points}

        self.points += points
        self.total_points += points
        old_level = self.level
        self.level = self.calculate_level()
        self.updated_at = datetime.utcnow()

        transaction = PointTransaction(
            student_id=self.student_id,
            points=points,
            reason=reason,
            level_before=old_level,
            level_after=self.level
        )
        db.session.add(transaction)

        today = datetime.utcnow().date()
        if self.last_active == today - timedelta(days=1):
            self.streak_days += 1
        elif self.last_active != today:
            self.streak_days = 1
        elif (self.streak_days or 0) == 0:
            # أول نشاط في اليوم نفسه الذي أُنشئ فيه الملف (last_active == today):
            # يجب أن يبدأ التتابع من 1 بدل البقاء صفراً رغم النشاط.
            self.streak_days = 1
        self.last_active = today

        db.session.commit()
        return {"level_up": self.level > old_level, "new_level": self.level, "new_points": self.points}

    def deduct_points(self, points, reason="خصم"):
        if points <= 0:
            return
        self.points -= points
        if self.points < 0:
            self.points = 0
        old_level = self.level
        self.level = self.calculate_level()
        self.updated_at = datetime.utcnow()
        transaction = PointTransaction(
            student_id=self.student_id,
            points=-points,
            reason=reason,
            level_before=old_level,
            level_after=self.level
        )
        db.session.add(transaction)
        db.session.commit()
        return {"level_down": self.level < old_level, "new_level": self.level, "new_points": self.points}

    def to_dict(self):
        return {
            "student_id": self.student_id,
            "level": self.level,
            "points": self.points,
            "total_points": self.total_points,
            "streak_days": self.streak_days,
            "show_in_leaderboard": self.show_in_leaderboard,
            "level_progress": (self.points % 100) / 100 * 100 if self.points > 0 else 0,
            "next_level_points": (math.floor(self.points / 100) + 1) * 100
        }

class PointTransaction(db.Model):
    __tablename__ = 'point_transactions'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    points = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    level_before = db.Column(db.Integer)
    level_after = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    student = db.relationship('Student')

    def to_dict(self):
        return {
            "id": self.id,
            "points": self.points,
            "reason": self.reason,
            "level_before": self.level_before,
            "level_after": self.level_after,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

# ==========================================
# نظام «تعلم اللغة مع Amr» — تعلم الإنجليزية
# ==========================================
# لا يعتمد على أي خدمة ذكاء اصطناعي. الكلمات تُستورد من CSV عبر
# /admin/import-language-words أو سكربت seed_language_words.py.
# XP يُمنح فقط عبر UserProfile.add_points (مرة واحدة لكل كلمة + مرة
# واحدة يومياً لإكمال الدرس) — والسبب الفريد هو ما يضمن عدم التكرار
# عبر خاصية إزالة التكرار الموجودة في add_points.
LANGUAGE_TRACKS = ('medical', 'engineering', 'general')
LANGUAGE_DAILY_TARGETS = (5, 10, 20)
LANGUAGE_WORD_XP = 2    # إتقان كلمة جديدة لأول مرة
LANGUAGE_LESSON_XP = 10  # إكمال درس اليوم

class LanguageWord(db.Model):
    """كلمة إنجليزية واحدة ضمن مسار (طبي / هندسي / عام)."""
    __tablename__ = 'language_words'
    __table_args__ = (db.UniqueConstraint('track', 'word', name='uq_language_word_track_word'),)
    id = db.Column(db.Integer, primary_key=True)
    track = db.Column(db.String(20), nullable=False)  # medical / engineering / general
    word = db.Column(db.String(150), nullable=False)
    translation = db.Column(db.String(300), nullable=False)
    sentence = db.Column(db.Text, nullable=True)
    sentence_translation = db.Column(db.Text, nullable=True)
    pos = db.Column(db.String(50), nullable=True)  # part of speech
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, state=None):
        return {
            "id": self.id,
            "word": self.word,
            "translation": self.translation,
            "sentence": self.sentence or "",
            "sentence_translation": self.sentence_translation or "",
            "pos": self.pos or "",
            "state": state,
        }

class UserLanguageProfile(db.Model):
    """ملف اللغة الخاص بطالب: المسار + الهدف اليومي + التتابع."""
    __tablename__ = 'user_language_profiles'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), unique=True, nullable=False)
    track = db.Column(db.String(20), default='general', nullable=False)
    daily_target = db.Column(db.Integer, default=10, nullable=False)
    streak_days = db.Column(db.Integer, default=0, nullable=False)
    last_lesson_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class LanguagePlan(db.Model):
    """خطة تعلم لغة واحدة: مسار + هدف يومي. للطالب عدة خطط (طب +
    هندسة + عام)، وواحدة منها فقط هي الحالية (is_current). التقدم
    والـ XP يبقيان على مستوى الطالب/الكلمة (UserLanguageWord)، فحذف
    خطة لا يمسح كلمات متقَنة ولا نقاطاً."""
    __tablename__ = 'language_plans'
    __table_args__ = (db.UniqueConstraint('student_id', 'track', name='uq_language_plan_student_track'),)
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    track = db.Column(db.String(20), nullable=False)  # medical / engineering / general
    daily_target = db.Column(db.Integer, default=10, nullable=False)
    is_current = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, mastered=None, total_in_track=None):
        return {
            "id": self.id,
            "track": self.track,
            "daily_target": self.daily_target,
            "is_current": bool(self.is_current),
            "mastered": mastered if mastered is not None else 0,
            "total_in_track": total_in_track if total_in_track is not None else 0,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
        }

class UserLanguageWord(db.Model):
    """حالة طالب مع كلمة: رآها / أتقنها، مع عدّادات المحاولات."""
    __tablename__ = 'user_language_words'
    __table_args__ = (db.UniqueConstraint('student_id', 'word_id', name='uq_user_language_word'),)
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    word_id = db.Column(db.Integer, db.ForeignKey('language_words.id'), nullable=False)
    state = db.Column(db.String(20), default='seen', nullable=False)  # seen / mastered
    correct_count = db.Column(db.Integer, default=0, nullable=False)
    seen_count = db.Column(db.Integer, default=0, nullable=False)
    last_seen = db.Column(db.DateTime, nullable=True)
    mastered_at = db.Column(db.DateTime, nullable=True)

def _default_language_track(user):
    """المسار الافتراضي من كلية الطالب: الطب → طبي، الهندسة → هندسي،
    غير ذلك (أو بلا كلية) → عام."""
    college = (getattr(user, 'college', '') or '').strip()
    if college == 'كلية الطب':
        return 'medical'
    if college == 'كلية الهندسة':
        return 'engineering'
    return 'general'

def _get_or_create_language_profile(user):
    profile = UserLanguageProfile.query.filter_by(student_id=user.id).first()
    if not profile:
        profile = UserLanguageProfile(student_id=user.id, track=_default_language_track(user))
        db.session.add(profile)
        db.session.commit()
    return profile

def _ensure_language_plan(user):
    """الخطة الحالية للطالب، مع إنشائها عند غيابها. ترحيل: إن كان
    للطالب ملف لغة قديم (UserLanguageProfile) بلا خطط، تُنشأ خطة
    واحدة من مساره وهدفه وتُجعل حالية. وإن لم يوجد ملف أصلاً، يُنشأ
    الملف أولاً (بالمسار الافتراضي من الكلية) ثم الخطة منه.
    تُستخدم من مسارات الدرس/الإجابة/الإكمال/التقدم — وليس من
    GET /api/language/plans، حتى يرى الطالب الجديد بطاقة إنشاء
    الخطة أولاً بدل أن تُنشأ له خطة تلقائياً."""
    plans = LanguagePlan.query.filter_by(student_id=user.id).order_by(LanguagePlan.id).all()
    if plans:
        current = next((p for p in plans if p.is_current), None)
        if current is None:
            current = plans[0]
            current.is_current = True
            db.session.commit()
        return current
    profile = _get_or_create_language_profile(user)
    plan = LanguagePlan(student_id=user.id, track=profile.track,
                        daily_target=profile.daily_target, is_current=True)
    db.session.add(plan)
    db.session.commit()
    return plan

def _sync_profile_to_plan(user, plan):
    """مرآة الخطة الحالية في ملف اللغة القديم (track/daily_target)،
    حتى تبقى أي مسارات قديمة تقرأ الملف صحيحة."""
    profile = _get_or_create_language_profile(user)
    profile.track = plan.track
    profile.daily_target = plan.daily_target
    profile.updated_at = datetime.utcnow()
    db.session.commit()
    return profile

def _plans_with_progress(user):
    """كل خطط الطالب مع تقدم كل خطة (متقَن / إجمالي مسارها)."""
    plans = LanguagePlan.query.filter_by(student_id=user.id).order_by(LanguagePlan.id).all()
    totals = _word_counts_by_track()
    return [p.to_dict(mastered=_mastered_count(user.id, p.track),
                      total_in_track=totals.get(p.track, 0)) for p in plans]

def _get_or_create_user_profile(student_id):
    """UserProfile العام (حامل XP) — يُنشأ عند غيابه حتى لا يفشل منح XP."""
    profile = UserProfile.query.filter_by(student_id=student_id).first()
    if not profile:
        profile = UserProfile(student_id=student_id)
        db.session.add(profile)
        db.session.commit()
    return profile

def _language_level(mastered_count):
    """مستوى اللغة: كل 100 كلمة متقَنة = مستوى. 0-99 → 1، 100-199 → 2 ..."""
    return math.floor((mastered_count or 0) / 100) + 1

def _mastered_count(student_id, track):
    return (db.session.query(UserLanguageWord)
            .join(LanguageWord, UserLanguageWord.word_id == LanguageWord.id)
            .filter(UserLanguageWord.student_id == student_id,
                    UserLanguageWord.state == 'mastered',
                    LanguageWord.track == track)
            .count())

def _word_counts_by_track():
    counts = {t: 0 for t in LANGUAGE_TRACKS}
    for track, n in db.session.query(LanguageWord.track, func.count(LanguageWord.id)).group_by(LanguageWord.track).all():
        if track in counts:
            counts[track] = n
    return counts

def _xp_from_language(student_id):
    """مجموع XP الممنوح من اللغة فقط (حسب سببي المنح المعروفين)."""
    total = (db.session.query(func.coalesce(func.sum(PointTransaction.points), 0))
             .filter(PointTransaction.student_id == student_id)
             .filter((PointTransaction.reason.like('تعلم كلمة إنجليزية #%'))
                     | (PointTransaction.reason.like('إكمال درس اللغة %')))
             .scalar())
    return int(total or 0)

def _language_profile_payload(user, lang_profile):
    """حمولة الملف: المسار والهدف اليومي من الخطة الحالية للطالب،
    والتتابع ونقاط اللغة من ملفه (مشتركة بين كل خططه)."""
    plan = _ensure_language_plan(user)
    mastered = _mastered_count(user.id, plan.track)
    total_in_track = LanguageWord.query.filter_by(track=plan.track).count()
    return {
        "track": plan.track,
        "daily_target": plan.daily_target,
        "mastered": mastered,
        "level": _language_level(mastered),
        "level_progress": mastered % 100,
        "streak": lang_profile.streak_days or 0,
        "total_in_track": total_in_track,
        "xp_from_language": _xp_from_language(user.id),
        "current_plan_id": plan.id,
    }

def import_language_words_csv(file_storage_or_path, default_track=None):
    """استيراد كلمات لغة من CSV — مشترك بين رفع المسؤول وسكربت البذر.
    يقبل: مسار ملف، أو FileStorage من Flask، أو كائن ملف مفتوح.
    الأعمدة: word,translation,sentence,sentence_translation,track,pos.
    يُتخطى الصف إن فرغت word/translation أو كان track غير صالح،
    وتُتخطى المكررات (نفس track+word) دون خطأ.
    يعيد {"imported": n, "skipped": n, "skipped_invalid": n}.
    default_track: إن وُجد يُستخدم للصفوف بلا track (ملفات البذر
    المخصصة لمسار واحد)؛ بدونه الصف بلا track غير صالح."""
    if isinstance(file_storage_or_path, (str, os.PathLike)):
        with open(file_storage_or_path, 'rb') as fh:
            raw = fh.read()
    elif hasattr(file_storage_or_path, 'read'):
        raw = file_storage_or_path.read()
        if hasattr(file_storage_or_path, 'seek'):
            try:
                file_storage_or_path.seek(0)
            except Exception:
                pass
    else:
        raise ValueError("مصدر CSV غير مدعوم")
    if isinstance(raw, bytes):
        text = raw.decode('utf-8-sig')
    else:
        text = raw
    reader = csv.DictReader(io.StringIO(text, newline=None))
    imported = 0
    skipped = 0
    skipped_invalid = 0
    seen_in_file = set()
    existing = {(w.track, w.word) for w in
                db.session.query(LanguageWord.track, LanguageWord.word).all()}
    for row in reader:
        word = (row.get('word') or '').strip()
        translation = (row.get('translation') or '').strip()
        track = (row.get('track') or '').strip() or (default_track or '')
        if not word or not translation or track not in LANGUAGE_TRACKS:
            skipped_invalid += 1
            continue
        key = (track, word)
        if key in existing or key in seen_in_file:
            skipped += 1
            continue
        seen_in_file.add(key)
        db.session.add(LanguageWord(
            track=track,
            word=word,
            translation=translation,
            sentence=(row.get('sentence') or '').strip() or None,
            sentence_translation=(row.get('sentence_translation') or '').strip() or None,
            pos=(row.get('pos') or '').strip() or None,
        ))
        imported += 1
    db.session.commit()
    return {"imported": imported, "skipped": skipped, "skipped_invalid": skipped_invalid}

def _seed_language_words():
    """بذر/تحديث كلمات اللغة من ملفات CSV المرفقة عند كل تشغيل:
    يستورد الصفوف الجديدة فقط (import_language_words_csv يتخطى
    أزواج (track, word) الموجودة)، فيعمل على جدول فارغ وعلى جدول
    يحوي صفوفاً قديمة — فإضافة كلمات جديدة للملفات تكفي لاستيرادها
    عند النشر التالي دون مسح الجدول. يعيد عدد الكلمات المستوردة."""
    seeded = 0
    app_dir = os.path.dirname(os.path.abspath(__file__))
    for fname, track in (("lang_seed_medical.csv", "medical"),
                         ("lang_seed_engineering.csv", "engineering"),
                         ("lang_seed_general.csv", "general")):
        fpath = os.path.join(app_dir, fname)
        if os.path.exists(fpath):
            res = import_language_words_csv(fpath, default_track=track)
            seeded += res["imported"]
    return seeded

class UnifiedTask(db.Model):
    __tablename__ = 'unified_tasks'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    planned_date = db.Column(db.Date, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    priority = db.Column(db.Integer, default=2)
    source = db.Column(db.String(20), default='manual')
    allocated_hours = db.Column(db.Float, default=1.0)
    is_wird = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship('Student', backref='unified_tasks')
    lesson = db.relationship('CustomLesson')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "title": self.title,
            "description": self.description or "",
            "planned_date": self.planned_date.strftime("%Y-%m-%d") if self.planned_date else "",
            "completed": self.completed,
            "priority": self.priority,
            "source": self.source,
            "allocated_hours": self.allocated_hours or 1.0,
            "is_wird": self.is_wird,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else ""
        }

class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='favorites')
    lesson = db.relationship('CustomLesson', backref='favorited_by')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson": self.lesson.to_dict() if self.lesson else None,
            "added_at": self.added_at.strftime("%Y-%m-%d %H:%M") if self.added_at else ""
        }

class Progress(db.Model):
    __tablename__ = 'progress'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    status = db.Column(db.String(20), default='not_started')
    progress_percent = db.Column(db.Integer, default=0)
    last_accessed = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='progress_records')
    lesson = db.relationship('CustomLesson', backref='progress_records')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson_title": self.lesson.title if self.lesson else "",
            "status": self.status,
            "progress_percent": self.progress_percent,
            "last_accessed": self.last_accessed.strftime("%Y-%m-%d %H:%M") if self.last_accessed else ""
        }

class LessonQuestion(db.Model):
    __tablename__ = 'lesson_questions'
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    student_name = db.Column(db.String(100), nullable=False, default="طالب")
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text, nullable=True)
    is_answered = db.Column(db.Boolean, default=False)
    answered_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    answered_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        date_str = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "student_name": self.student_name,
            "question_text": self.question_text,
            "answer_text": self.answer_text,
            "is_answered": self.is_answered,
            "created_at": date_str
        }

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default="إضافة جديدة")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        date_str = self.created_at.strftime("%Y-%m-%d") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d")
        return {
            "id": self.id,
            "title": self.title,
            "tag": self.tag,
            "description": self.description,
            "date": date_str
        }

class CustomLesson(db.Model):
    __tablename__ = 'custom_lessons'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    college = db.Column(db.String(150), nullable=True)
    subject_name = db.Column(db.String(150), nullable=False)
    subject_id = db.Column(db.Integer, nullable=True)  # رابط المادة ككيان (جدول subjects)؛ subject_name يبقى نصاً منسوخاً للتوافق
    chapter_name = db.Column(db.String(150), nullable=False)
    content_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=True)
    download_url = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    is_vip = db.Column(db.Boolean, default=False)  # محتوى مدفوع: مغلق لمن ليس لديه منح تغطيه
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        date_str = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        return {
            "id": self.id,
            # عرض فقط: تطبيع إملاء السنة المخزنة (انظر _display_year).
            "year": _display_year(self.year),
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "content_type": self.content_type,
            "title": self.title,
            "url": self.url or "",
            "download_url": self.download_url or "",
            "notes": self.notes or "",
            "status": self.status or 'approved',
            "is_vip": bool(self.is_vip),
            "created_by": self.created_by,
            "created_at": date_str
        }

class QuizQuestion(db.Model):
    __tablename__ = 'quiz_questions'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    college = db.Column(db.String(150), nullable=True)
    subject_name = db.Column(db.String(150), nullable=False)
    subject_id = db.Column(db.Integer, nullable=True)  # رابط المادة ككيان (جدول subjects)؛ subject_name يبقى نصاً منسوخاً للتوافق
    chapter_name = db.Column(db.String(150), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.Text, nullable=True)
    option_b = db.Column(db.Text, nullable=True)
    option_c = db.Column(db.Text, nullable=True)
    option_d = db.Column(db.Text, nullable=True)
    option_e = db.Column(db.Text, nullable=True)
    correct_answer = db.Column(db.String(200), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        q_text = (self.question_text or '').strip()
        opts = [self.option_a, self.option_b, self.option_c, self.option_d, self.option_e]
        cleaned_opts = []
        for o in opts:
            if o and o.strip() and o.strip() != q_text:
                cleaned_opts.append(o.strip())

        # قد تُستورد correct_answer كحرف (A-E) أو رقم (1-5) بدل نص الخيار؛
        # نحوّلها إلى نص الخيار المطابق حتى تقارنها الواجهة بشكل صحيح.
        # ملاحظة: التحويل يتم على cleaned_opts بترتيب الأعمدة المخزنة الأصلي
        # (المؤشر 0 = option_a)، لأن بعض الصفوف كُتبت حروفها على هذا الأساس.
        correct = (self.correct_answer or "").strip()
        if len(correct) == 1 and correct.upper() in "ABCDE":
            idx = "ABCDE".index(correct.upper())
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]
        elif len(correct) == 1 and correct in "12345":
            idx = int(correct) - 1
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]

        # خلل الاستيراد القديم: كل الأسئلة المستوردة تحمل question_text = "اختبار"
        # ونص السؤال الحقيقي في option_a، فتظهر الواجهة "اختبار" عنواناً والسؤال
        # نفسه زر إجابة أول. نعرض نص السؤال الحقيقي عنواناً والخيارات الحقيقية
        # فقط، دون تغيير البيانات المخزنة أو إزاحة الحقول.
        display_q = q_text
        display_opts = cleaned_opts
        if _is_placeholder_question_title(q_text) and cleaned_opts:
            display_q = cleaned_opts[0]
            display_opts = cleaned_opts[1:]

        return {
            "id": self.id,
            # عرض فقط: تطبيع إملاء السنة المخزنة (انظر _display_year).
            "year": _display_year(self.year),
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "question_text": display_q,
            "option_a": display_opts[0] if len(display_opts) > 0 else "",
            "option_b": display_opts[1] if len(display_opts) > 1 else "",
            "option_c": display_opts[2] if len(display_opts) > 2 else "",
            "option_d": display_opts[3] if len(display_opts) > 3 else "",
            "option_e": display_opts[4] if len(display_opts) > 4 else "",
            "correct_answer": correct,
            "explanation": self.explanation or "",
            "status": self.status or 'approved'
        }

class QuizAttempt(db.Model):
    __tablename__ = 'quiz_attempts'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=True)
    score = db.Column(db.Integer, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    percentage = db.Column(db.Float, nullable=False)
    xp_awarded = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "score": self.score,
            "total_questions": self.total_questions,
            "percentage": self.percentage,
            "xp_awarded": self.xp_awarded,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

with app.app_context():
    try:
        db.create_all()
        # ترحيل أعمدة جديدة لجداول قائمة: create_all() ينشئ الجداول الناقصة فقط
        # ولا يضيف أعمدة لجداول موجودة، فأي استعلام على Student كان سيسقط التشغيل
        # (خطأ: column students.supervisor_college does not exist).
        try:
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_college VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_specialization VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE custom_lessons ADD COLUMN IF NOT EXISTS college VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS college VARCHAR(150)"))
            # أعمدة ربط المواد ككيانات: create_all لا يضيف أعمدة لجداول موجودة
            db.session.execute(db.text("ALTER TABLE custom_lessons ADD COLUMN IF NOT EXISTS subject_id INTEGER"))
            db.session.execute(db.text("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS subject_id INTEGER"))
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_subject_id INTEGER"))
            # أعمدة «خدمات» الجديدة: create_all() لا يضيف أعمدة لجداول موجودة،
            # والمنتجات القديمة بلا عملة تُعامل كريال يمني (YER).
            db.session.execute(db.text("ALTER TABLE stores ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)"))
            db.session.execute(db.text("ALTER TABLE products ADD COLUMN IF NOT EXISTS currency VARCHAR(10)"))
            db.session.execute(db.text("UPDATE products SET currency = 'YER' WHERE currency IS NULL"))
            # علم «محتوى VIP» على الدروس: create_all() لا يضيف أعمدة لجداول
            # موجودة، والقديم كله مجاني (False).
            db.session.execute(db.text("ALTER TABLE custom_lessons ADD COLUMN IF NOT EXISTS is_vip BOOLEAN"))
            db.session.execute(db.text("UPDATE custom_lessons SET is_vip = FALSE WHERE is_vip IS NULL"))
            # توسيع عمود الدور إلى VARCHAR(50): قيمة specialization_supervisor طولها 25 حرفاً
            # فلا تتسع في VARCHAR(20) وكان الحفظ يفشل بخطأ 500. يُتخطى على SQLite، وآمن عند
            # إعادة التشغيل لأن تعيين نفس النوع مجدداً لا يغيّر شيئاً.
            if "sqlite" not in db_url:
                db.session.execute(db.text("ALTER TABLE students ALTER COLUMN role TYPE VARCHAR(50)"))
            db.session.commit()
            # تحديث قيد فحص الدور: القيد القديم students_role_check يسمح فقط بالأدوار
            # القديمة ويرفض college_supervisor وspecialization_supervisor (خطأ 23514
            # عند تعيين مشرف). يُتخطى على SQLite، وآمن عند إعادة التشغيل لأن
            # DROP IF EXISTS ثم ADD من جديد يعيدان إنشاء القيد في كل مرة.
            if "sqlite" not in db_url:
                try:
                    db.session.execute(db.text("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_role_check"))
                    db.session.commit()
                    print("تم حذف قيد الدور القديم students_role_check", flush=True)
                except Exception as _role_drop_e:
                    db.session.rollback()
                    print(f"تعذر حذف قيد الدور القديم (تجاهل): {_role_drop_e}", flush=True)
                try:
                    db.session.execute(db.text(
                        "ALTER TABLE students ADD CONSTRAINT students_role_check "
                        "CHECK (role IN ('student', 'assistant', 'admin', 'college_supervisor', 'specialization_supervisor', 'store_manager'))"
                    ))
                    db.session.commit()
                    print("تم تحديث قيد الدور students_role_check ليشمل أدوار المشرفين ومدير المتجر", flush=True)
                except Exception as _role_add_e:
                    db.session.rollback()
                    print(f"تعذر تحديث قيد الدور (تجاهل): {_role_add_e}", flush=True)
        except Exception as _mig_e:
            db.session.rollback()
            print(f"تعذر ترحيل الأعمدة الجديدة (تجاهل — قد تكون قاعدة بيانات محلية): {_mig_e}", flush=True)
        students = Student.query.all()
        for s in students:
            if not UserProfile.query.filter_by(student_id=s.id).first():
                profile = UserProfile(student_id=s.id)
                db.session.add(profile)

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        # لا تبتلع الخطأ بصمت: اطبعه ثم أعد رفعه ليفشل التشغيل بسرعة بدل العمل بجداول ناقصة
        print(f"خطأ فادح عند إنشاء جداول قاعدة البيانات: {e}", flush=True)
        raise

# بذر الكليات الافتراضية وربط المحتوى القديم بكلية الهندسة.
# كتلة مستقلة محمية بـ try/except حتى لا يُسقط أي خطأ بياناتٍ التشغيل أبداً.
with app.app_context():
    try:
        for _cname in ("كلية الهندسة", "كلية الطب"):
            if not College.query.filter_by(name=_cname).first():
                db.session.add(College(name=_cname))
        db.session.commit()
        CustomLesson.query.filter((CustomLesson.college == None) | (CustomLesson.college == "")).update({"college": "كلية الهندسة"}, synchronize_session=False)
        QuizQuestion.query.filter((QuizQuestion.college == None) | (QuizQuestion.college == "")).update({"college": "كلية الهندسة"}, synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"تعذر بذر الكليات وربط المحتوى القديم: {e}", flush=True)

# بذر كلمات «تعلم اللغة مع Amr» عند كل تشغيل (يستورد الجديد فقط).
# ملفات CSV المرفقة في المستودع هي بنك الكلمات (3000 كلمة لكل مسار).
# خامل: يستورد الصفوف الجديدة فقط، ومحمي بـ try/except حتى لا يُسقط أي
# خطأ بياناتٍ التشغيل أبداً. النمو لاحقاً عبر إضافة صفوف للملفات
# (تُستورد عند النشر التالي) أو رفع المسؤول /admin/import-language-words.
with app.app_context():
    try:
        _seeded = _seed_language_words()
        if _seeded:
            print(f"تم بذر كلمات اللغة: {_seeded} كلمة", flush=True)
    except Exception as _lang_seed_e:
        db.session.rollback()
        print(f"تعذر بذر كلمات اللغة (تجاهل): {_lang_seed_e}", flush=True)

# بذر المواد من المحتوى الحالي وربط الدروس والأسئلة بها.
# قبل هذا كانت المادة مجرد نص على كل صف؛ الآن كل (كلية، سنة، ترم، اسم مادة)
# مميز يصبح صفاً في جدول subjects، وتربط الصفوف به عبر subject_id.
# الدروس بلا اسم مادة تربط بمادة «عام» (Q3). كتلة مستقلة محمية بـ try/except
# حتى لا يُسقط أي خطأ بياناتٍ التشغيل أبداً، وهي خاملة (idempotent) عند إعادة التشغيل.
with app.app_context():
    try:
        _subject_keys = set()
        for _model in (CustomLesson, QuizQuestion):
            for _college, _year, _term, _sname in db.session.query(
                    _model.college, _model.year, _model.term, _model.subject_name).distinct().all():
                _college = (_college or '').strip() or 'كلية الهندسة'
                _year = (_year or '').strip()
                _term = (_term or '').strip()
                _sname = (_sname or '').strip() or 'عام'
                if not _year or not _term:
                    continue
                _key = (_college, _year, _term, _sname)
                if _key in _subject_keys:
                    continue
                _subject_keys.add(_key)
                if not Subject.query.filter_by(college=_college, year=_year, term=_term, name=_sname).first():
                    db.session.add(Subject(name=_sname, college=_college, year=_year, term=_term))
        db.session.commit()
        # ربط الصفوف التي بلا subject_id بمادتها المطابقة
        for _model in (CustomLesson, QuizQuestion):
            for _row in _model.query.filter(_model.subject_id == None).all():
                _college = (_row.college or '').strip() or 'كلية الهندسة'
                _year = (_row.year or '').strip()
                _term = (_row.term or '').strip()
                _sname = (_row.subject_name or '').strip() or 'عام'
                if not _year or not _term:
                    continue
                _subj = Subject.query.filter_by(college=_college, year=_year, term=_term, name=_sname).first()
                if _subj:
                    _row.subject_id = _subj.id
        db.session.commit()
        print("تم بذر المواد وربط الدروس والأسئلة بها", flush=True)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("تعذر بذر المواد وربطها")
        print(f"تعذر بذر المواد وربطها: {e}", flush=True)


# ==========================================
# Helpers & Auth Decorators
# ==========================================
def get_current_user():
    uid = session.get('student_id')
    if not uid:
        return None
    return Student.query.get(uid)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role != 'admin':
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب مسؤول)"}), 403
        return f(*args, **kwargs)
    return decorated_function

SUPERVISOR_ROLES = ('college_supervisor', 'specialization_supervisor')

def _in_scope(user, college, subject_name=None, subject_id=None):
    """هل هذا العنصر (كلية/تخصص) ضمن نطاق المستخدم؟ المشرف الأعلى يرى كل شيء.
    لمشرف التخصص: إن وُجد supervisor_subject_id يُقارن به بدقة، وإلا يُقارن بالاسم."""
    if not user:
        return False
    if user.role == 'admin':
        return True
    if user.role == 'college_supervisor':
        return (user.supervisor_college or '') == (college or '')
    if user.role == 'specialization_supervisor':
        if subject_id is not None and getattr(user, 'supervisor_subject_id', None):
            return user.supervisor_subject_id == subject_id
        return ((user.supervisor_college or '') == (college or '')
                and (user.supervisor_specialization or '') == (subject_name or ''))
    return False

def _scope_query(query, user, model):
    """فلترة استعلام محتوى (CustomLesson أو QuizQuestion) حسب نطاق المستخدم.
    المشرف الأعلى والمساعد يبقيان على سلوكهما الحالي (الفلترة تتم في المسار نفسه)."""
    if not user or user.role == 'admin' or user.role == 'assistant':
        return query
    if user.role == 'college_supervisor':
        return query.filter(model.college == (user.supervisor_college or ''))
    if user.role == 'specialization_supervisor':
        if getattr(user, 'supervisor_subject_id', None):
            return query.filter(model.subject_id == user.supervisor_subject_id)
        return query.filter(model.college == (user.supervisor_college or ''),
                            model.subject_name == (user.supervisor_specialization or ''))
    return query

def _subject_college_info(subject_id):
    """كلية واسم مادة معرّف مادة ما (year_term_subject) من صفوف الدروس أو الأسئلة."""
    for model in (CustomLesson, QuizQuestion):
        for row in model.query.with_entities(model.year, model.term, model.subject_name, model.college).all():
            sid = f"{row.year}_{row.term}_{row.subject_name}".replace(" ", "_")
            if sid == subject_id:
                return (row.college or "كلية الهندسة", row.subject_name)
    return None

def _question_in_scope(q, user):
    """هل سؤال درس ما ضمن نطاق المشرف؟"""
    info = _subject_college_info(q.subject_id)
    if not info:
        return False
    college, subject_name = info
    return _in_scope(user, college, subject_name)

def admin_or_supervisor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ('admin',) + SUPERVISOR_ROLES:
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب مسؤول)"}), 403
        return f(*args, **kwargs)
    return decorated_function

def can_manage_content(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ['admin', 'assistant'] + list(SUPERVISOR_ROLES):
            return jsonify({"status": "error", "message": "غير مصرح لك بإدارة المحتوى"}), 403
        return f(*args, **kwargs)
    return decorated_function

def store_manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ('admin', 'store_manager'):
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب مدير متجر)"}), 403
        return f(*args, **kwargs)
    return decorated_function

def _can_manage_store(user, store):
    """هل يستطيع هذا المستخدم إدارة هذا المتجر؟ الأدمن كل شيء، ومدير المتجر متجره فقط."""
    if not user or not store:
        return False
    if user.role == 'admin':
        return True
    if user.role == 'store_manager':
        return store.manager_id == user.id
    return False

def normalize_whatsapp(raw):
    """تطبيع رقم واتساب لـ wa.me: إزالة + والمسافات والشرطات، ثم بادئة اليمن 967.
    0xxxxxxxxx -> 967xxxxxxxxx ، 7xxxxxxxx -> 9677xxxxxxxx ، 00967... -> 967..."""
    digits = re.sub(r'[^0-9]', '', (raw or '').strip())
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('0'):
        digits = '967' + digits[1:]
    elif digits.startswith('7'):
        digits = '967' + digits
    return digits

def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('student_id'):
            return jsonify({"status": "error", "message": "يرجى تسجيل الدخول أولاً"}), 401
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# نظام VIP — الوصول المدفوع (كل شيء مجاني افتراضياً)
# ==========================================
# مفاتيح خدمات الفقاعة القابلة للإغلاق كـ VIP + أسماؤها المعروضة.
VIP_SERVICE_KEYS = {
    'calculator': 'حاسبة المعدل',
    'bot': 'المساعد الذكي',
    'planner': 'الخطة الدراسية',
    'simulator': 'المختبر',
    'guide': 'دليل الطالب',
}
VIP_SERVICE_KEY_LIST = tuple(VIP_SERVICE_KEYS.keys())
# رقم التواصل المستخدم في رسائل طلب تفعيل VIP (نفس رقم زر الدعم الفني).
SUPPORT_WHATSAPP = '967774485611'

def _vip_grant_is_active(grant):
    """هل المنح ما زال سارياً؟ NULL في expires_at تعني أبدي."""
    if grant is None:
        return False
    return grant.expires_at is None or grant.expires_at > datetime.utcnow()

def _has_vip_grant(user, scope_type, scope_key):
    """هل لدى المستخدم منح VIP نشط يغطي الهدف؟ الأدمن والمساعد معفيان."""
    if not user:
        return False
    if user.role in ('admin', 'assistant'):
        return True
    now = datetime.utcnow()
    grants = VipGrant.query.filter_by(student_id=user.id, scope_type=scope_type, scope_key=str(scope_key)).all()
    return any(g.expires_at is None or g.expires_at > now for g in grants)

def _lesson_vip_locked(user, lesson):
    """هل هذا الدرس VIP ومغلق أمام المستخدم؟ منح محتوى على الدرس نفسه أو
    منح مادة على مادته تفتحه. الدرس غير المعلَّم VIP مفتوح دائماً."""
    if not lesson or not getattr(lesson, 'is_vip', False):
        return False
    if not user:
        return True
    if user.role in ('admin', 'assistant'):
        return False
    if _has_vip_grant(user, 'content', lesson.id):
        return False
    if getattr(lesson, 'subject_id', None) and _has_vip_grant(user, 'subject', lesson.subject_id):
        return False
    return True

def _quiz_chapter_locked(user, subject_id=None, subject_name=None, chapter_name=None, college=None):
    """هل اختبار هذا الفصل مغلق؟ الفصل مغلق إن وُجد درس اختبار VIP فيه
    (CustomLesson من نوع quiz) بلا منح تغطيه. لا درس اختبار VIP = مفتوح."""
    if not chapter_name:
        return False
    if user and user.role in ('admin', 'assistant'):
        return False
    q = CustomLesson.query.filter_by(content_type='quiz', chapter_name=chapter_name, is_vip=True)
    if subject_id:
        q = q.filter_by(subject_id=subject_id)
    elif subject_name:
        q = q.filter_by(subject_name=subject_name)
        if college:
            q = q.filter_by(college=college)
    for les in q.all():
        if _lesson_vip_locked(user, les):
            return True
    return False

def _service_is_vip(key):
    row = VipService.query.get(key)
    return bool(row and row.is_vip)

def _service_locked(user, key):
    """هل الخدمة مغلقة كـ VIP أمام هذا المستخدم؟"""
    if not _service_is_vip(key):
        return False
    if not user:
        return True
    if user.role in ('admin', 'assistant'):
        return False
    return not _has_vip_grant(user, 'service', key)

def _vip_scope_label(scope_type, scope_key):
    """اسم معروض لهدف منح VIP (للوحة الإدارة والملف الشخصي)."""
    if scope_type == 'service':
        return VIP_SERVICE_KEYS.get(str(scope_key), str(scope_key))
    if scope_type == 'subject':
        try:
            s = Subject.query.get(int(scope_key))
            if s:
                return s.name
        except (TypeError, ValueError):
            pass
        return str(scope_key)
    if scope_type == 'content':
        try:
            les = CustomLesson.query.get(int(scope_key))
            if les:
                return les.title
        except (TypeError, ValueError):
            pass
        return str(scope_key)
    return str(scope_key)

def _serialize_vip_grant(grant):
    d = grant.to_dict()
    d["scope_label"] = _vip_scope_label(grant.scope_type, grant.scope_key)
    d["is_active"] = _vip_grant_is_active(grant)
    student = Student.query.get(grant.student_id)
    d["student_name"] = student.name if student else ""
    d["student_username"] = student.username if student else ""
    return d

@app.before_request
def _check_session_account_active():
    """إن حُظر حساب مسجَّل دخول أو حُذف، أول طلب تالٍ ينهي جلسته ويعرض
    له رسالة الحظر/الحذف. الصفحة الرئيسية تعرض الرسالة مرة واحدة عبر
    terminated_reason؛ مسارات API تعيدها JSON فوراً."""
    if request.path.startswith(('/static', '/audio/')) or request.path in ('/health',):
        return None
    uid = session.get('student_id')
    if not uid:
        return None
    user = Student.query.get(uid)
    if user and user.is_active:
        return None
    reason = 'banned' if (user and not user.is_active) else 'deleted'
    session.clear()
    msg = ('تم حظر هذا الحساب لمخالفة سياسة الموقع' if reason == 'banned'
           else 'تم حذف هذا الحساب لمخالفة سياسة الموقع')
    if request.path.startswith(('/api/', '/admin/')):
        return jsonify({"status": "error", "message": msg, "account_terminated": reason}), 401
    if request.method == 'GET' and request.path == '/':
        session['terminated_reason'] = reason
    return None

# ==========================================
# Platform Structured Data
# ==========================================
# تصحيح أسماء سنوات مخزنة بإملاء خاطئ في البيانات (تعرض كما هي في الواجهة)
YEAR_NAME_FIXES = {"السنة الاولئ": "السنة الأولى"}

# ترتيب عرض ثابت للسنوات والفصول (رقمي، لا أبجدي): الترتيب الأبجدي للنص
# العربي يضع «السنة الثالثة» قبل «السنة الثانية» فيعرض السنوات 1، 3، 2،
# وبغياب ORDER BY يعيد Postgres الصفوف بترتيب غير مضمون يتغير بين الطلبات.
CANONICAL_YEAR_ORDER = {
    'السنة الأولى': 1, 'السنة الثانية': 2, 'السنة الثالثة': 3,
    'السنة الرابعة': 4, 'السنة الخامسة': 5, 'السنة السادسة': 6,
}
CANONICAL_TERM_ORDER = {'الترم الأول': 1, 'الترم الثاني': 2}

def _display_year(value):
    """اسم السنة كما يُعرض للمستخدم (تطبيع إملائي للعرض فقط — لا يغيّر المخزن)."""
    return YEAR_NAME_FIXES.get((value or '').strip(), (value or '').strip())

def _year_order(name):
    """مفتاح ترتيب سنة: رقمها القياسي أولاً؛ السنوات المخصصة (مثل
    «سنة تحضيرية») بعد القياسية ومرتبة أبجدياً فيما بينها."""
    fixed = _display_year(name)
    return (CANONICAL_YEAR_ORDER.get(fixed, 99), fixed)

def _term_order(name):
    """مفتاح ترتيب ترم: الأول ثم الثاني، ثم أي قيمة أخرى أبجدياً."""
    n = (name or '').strip()
    return (CANONICAL_TERM_ORDER.get(n, 99), n)

def display_year(value):
    """سنة دراسية للعرض فقط: تطبيع الإملاء الخاطئ المخزن دون تغيير البيانات."""
    text = '' if value is None else str(value).strip()
    return YEAR_NAME_FIXES.get(text, text)

def display_name(value):
    """اسم مادة/فصل للعرض فقط (لا يمسّ البيانات ولا روابط البحث):
    يزيل نقطة زائدة في النهاية (مثل «Electronic.») ومسافات زائدة.
    لا يعيد تسمية أي شيء — القيم المستخدمة في الروابط والاستعلامات تبقى كما هي."""
    text = '' if value is None else str(value).strip()
    while text.endswith(('.', '。')):
        text = text[:-1].rstrip()
    return text

app.template_filter('display_name')(display_name)
app.template_filter('display_year')(display_year)

def get_platform_data(user):
    # البنية: كلية -> سنوات -> فصول دراسية -> مواد -> أنواع محتوى -> فصول -> دروس
    data = {}
    try:
        # الكليات المعروفة من جدول الكليات (مرتبة حسب الإضافة)؛
        # وأي كلية تظهر في محتوى قديم تُضاف تلقائياً حتى لا يضيع محتواها.
        try:
            known_colleges = [c.name for c in College.query.order_by(College.id).all()]
        except Exception:
            known_colleges = []
        if not known_colleges:
            known_colleges = ["كلية الهندسة"]
        for _cname in known_colleges:
            data[_cname] = {"name": _cname, "years": {}}

        # المواد ككيانات: نزرع كل مادة من جدول subjects أولاً حتى تظهر
        # المواد الفارغة أيضاً على الصفحة الرئيسية مع حالة فارغة (Q1).
        _subjects_by_id = {}
        try:
            _all_subjects = Subject.query.order_by(Subject.id).all()
        except Exception:
            db.session.rollback()
            _all_subjects = []
        for _s in _all_subjects:
            _s_college = (_s.college or '').strip() or "كلية الهندسة"
            _s_year = YEAR_NAME_FIXES.get((_s.year or '').strip(), (_s.year or '').strip())
            _s_term = (_s.term or '').strip()
            _s_name = (_s.name or '').strip()
            if not _s_year or not _s_term or not _s_name:
                continue
            _subjects_by_id[_s.id] = _s
            if _s_college not in data:
                data[_s_college] = {"name": _s_college, "years": {}}
            _s_years = data[_s_college]["years"]
            if _s_year not in _s_years:
                _s_years[_s_year] = {"name": _s_year, "terms": {}}
            if _s_term not in _s_years[_s_year]["terms"]:
                _s_years[_s_year]["terms"][_s_term] = {"name": _s_term, "subjects": {}}
            _s_subs = _s_years[_s_year]["terms"][_s_term]["subjects"]
            if _s_name not in _s_subs:
                _s_subs[_s_name] = {
                    "id": f"{_s_year}_{_s_term}_{_s_name}".replace(" ", "_"),
                    "name": _s_name, "year": _s_year, "term": _s_term, "college": _s_college,
                    "subject_id": _s.id, "lesson_count": 0, "video_count": 0, "file_count": 0,
                    "types": {
                        "videos": {"chapters": {}},
                        "files": {"chapters": {}},
                        "quiz": {"chapters": {}}
                    }
                }

        query = CustomLesson.query
        if not user or user.role == 'student':
            query = query.filter_by(status='approved')
        elif user.role == 'assistant':
            query = query.filter((CustomLesson.status == 'approved') | (CustomLesson.created_by == user.id))

        lessons = query.order_by(CustomLesson.id.asc()).all()
        quiz_query = QuizQuestion.query
        if not user or user.role == 'student':
            quiz_query = quiz_query.filter_by(status='approved')
        elif user.role == 'assistant':
            quiz_query = quiz_query.filter((QuizQuestion.status == 'approved') | (QuizQuestion.created_by == user.id))
        quiz_groups = quiz_query.with_entities(QuizQuestion.year, QuizQuestion.term, QuizQuestion.subject_name, QuizQuestion.chapter_name, QuizQuestion.college).distinct().all()
        for item in [*lessons, *quiz_groups]:
            # الدروس المرتبطة بمادة تُحلّ عبر subject_id (الكلية/السنة/الترم/
            # الاسم من صف المادة نفسه)؛ أما الصفوف بلا رابط فتُجمّع بالنص كما قبل.
            _linked = _subjects_by_id.get(item.subject_id) if isinstance(item, CustomLesson) else None
            if _linked is not None:
                college = (_linked.college or '').strip() or "كلية الهندسة"
                year = YEAR_NAME_FIXES.get((_linked.year or '').strip(), (_linked.year or '').strip())
                term = (_linked.term or '').strip()
                subject = (_linked.name or '').strip()
            else:
                year = (item.year or '').strip()
                year = YEAR_NAME_FIXES.get(year, year)
                term = (item.term or '').strip()
                subject = (item.subject_name or '').strip()
                college = (getattr(item, 'college', '') or '').strip() or "كلية الهندسة"
            chapter = (item.chapter_name or 'عام').strip() or 'عام'
            c_type = (getattr(item, 'content_type', 'quiz') or 'videos').strip()

            if not year or not term or not subject:
                continue

            if college not in data:
                data[college] = {"name": college, "years": {}}
            years = data[college]["years"]
            if year not in years:
                years[year] = {"name": year, "terms": {}}
            if term not in years[year]["terms"]:
                years[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in years[year]["terms"][term]["subjects"]:
                years[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject, "year": year, "term": term, "college": college,
                    "subject_id": _linked.id if _linked is not None else None, "lesson_count": 0, "video_count": 0, "file_count": 0,
                    "types": {
                        "videos": {"chapters": {}},
                        "files": {"chapters": {}},
                        "quiz": {"chapters": {}}
                    }
                }

            subj_data = years[year]["terms"][term]["subjects"][subject]
            
            if c_type not in subj_data["types"]:
                subj_data["types"][c_type] = {"chapters": {}}

            if chapter not in subj_data["types"][c_type]["chapters"]:
                subj_data["types"][c_type]["chapters"][chapter] = {"name": chapter, "lessons": []}

            if not isinstance(item, CustomLesson):
                continue
            lesson_dict = {
                "id": item.id,
                "title": item.title,
                "notes": item.notes or "",
                "status": item.status,
                "is_vip": bool(item.is_vip),
                "vip_locked": _lesson_vip_locked(user, item),
                "custom_id": item.id
            }
            if c_type == "videos":
                lesson_dict["video"] = item.url or ""
                lesson_dict["download"] = item.download_url or ""
            elif c_type == "files":
                lesson_dict["file"] = item.url or item.download_url or ""
                lesson_dict["download"] = item.download_url or ""
            if lesson_dict["vip_locked"]:
                # الروابط نفسها لا تظهر أبداً في الصفحة للدرس المقفل — الكتالوج
                # كاملاً مضمّن JSON في الصفحة للبحث، فأي رابط باقٍ فيه يُستخرج
                # من مصدر الصفحة ويفتح المحتوى المدفوع دون منح.
                lesson_dict["video"] = ""
                lesson_dict["file"] = ""
                lesson_dict["download"] = ""

            subj_data["types"][c_type]["chapters"][chapter]["lessons"].append(lesson_dict)
            subj_data["lesson_count"] = subj_data.get("lesson_count", 0) + 1
            if c_type == "videos":
                subj_data["video_count"] = subj_data.get("video_count", 0) + 1
            elif c_type == "files":
                subj_data["file_count"] = subj_data.get("file_count", 0) + 1

        # عدد أسئلة الاختبار لكل مادة (استعلامان مجمّعان فقط — لبطاقات المواد «N درس · M سؤال»)
        try:
            _qc = dict(db.session.query(QuizQuestion.subject_id, func.count(QuizQuestion.id)).filter(QuizQuestion.subject_id.isnot(None)).group_by(QuizQuestion.subject_id).all())
            _qc_legacy = {(r[0], r[1], r[2], r[3]): r[4] for r in db.session.query(QuizQuestion.college, QuizQuestion.year, QuizQuestion.term, QuizQuestion.subject_name, func.count(QuizQuestion.id)).filter(QuizQuestion.subject_id.is_(None)).group_by(QuizQuestion.college, QuizQuestion.year, QuizQuestion.term, QuizQuestion.subject_name).all()}
        except Exception:
            db.session.rollback()
            _qc, _qc_legacy = {}, {}

        for c in data.values():
            for y in c["years"].values():
                for t in y["terms"].values():
                    t["subjects"] = list(t["subjects"].values())
                    for s in t["subjects"]:
                        # شارة VIP على الفصل: أي درس VIP مغلق فيه يقفل الفصل كله
                        # (للفيديو/الملف تُقفل الدروس المغلقة فردياً داخل الفصل،
                        # وللاختبار يُقفل زر «بدء الاختبار» كاملاً).
                        for _ctype in ("videos", "files", "quiz"):
                            for _ch in s["types"][_ctype]["chapters"].values():
                                _ch_lessons = _ch.get("lessons", [])
                                _ch["has_vip"] = any(l.get("is_vip") for l in _ch_lessons)
                                _ch["vip_locked"] = any(l.get("vip_locked") for l in _ch_lessons)
                        s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                        s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                        s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())
                        s["question_count"] = _qc.get(s.get("subject_id"), 0) + _qc_legacy.get((s.get("college"), s.get("year"), s.get("term"), s.get("name")), 0)
                        # عدد الاختبارات = عدد فصول بنك الأسئلة في المادة (كل فصل اختبار مستقل)
                        s["test_count"] = len(s["types"]["quiz"]["chapters"])
                        s["has_vip"] = any(_ch.get("has_vip") for _ctype in ("videos", "files", "quiz") for _ch in s["types"][_ctype]["chapters"])
        # أعداد المواد لكل سنة/ترم (لبطاقات السنوات والفصول)
        for _c in data.values():
            for _y in _c["years"].values():
                _y["subject_count"] = sum(len(_t.get("subjects", [])) for _t in _y["terms"].values())
                for _t in _y["terms"].values():
                    _t["subject_count"] = len(_t.get("subjects", []))
        # ترتيب عرض ثابت: قواميس بايثون تحفظ ترتيب الإدراج، والسنوات كانت
        # تُدرَج بترتيب إدراج المواد (فيعرض 1، 3، 2) والكليات بترتيب ظهورها
        # في المحتوى؛ نعيد البناء هنا: الكليات بترتيب جدول الكليات (id)
        # ثم أي كلية أخرى أبجدياً، والسنوات رقمياً، والترم رقمياً، والمواد أبجدياً.
        _ordered_data = {}
        _college_names = [c for c in known_colleges if c in data]
        _college_names += sorted(c for c in data if c not in known_colleges)
        for _cname in _college_names:
            _c = data[_cname]
            _years = {}
            for _yname in sorted(_c.get("years", {}), key=_year_order):
                _y = _c["years"][_yname]
                _terms = {}
                for _tname in sorted(_y.get("terms", {}), key=_term_order):
                    _t = _y["terms"][_tname]
                    _t["subjects"] = sorted(_t.get("subjects", []), key=lambda s: (s.get("name") or ""))
                    _terms[_tname] = _t
                _y["terms"] = _terms
                _years[_yname] = _y
            _c["years"] = _years
            _ordered_data[_cname] = _c
        data = _ordered_data
    except Exception as e:
        db.session.rollback()

        return None

    return data

@app.after_request
def add_header(response):
    if request.path.startswith('/') and not request.path.startswith(('/static', '/audio/')):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# ==========================================
# Main Routes
# ==========================================

@app.route('/')
def home():
    user = get_current_user()
    data = get_platform_data(user)
    catalog_error = data is None
    data = data or {}

    is_admin = (user.role == 'admin') if user else False
    is_assistant = (user.role == 'assistant') if user else False
    is_supervisor = (user.role in SUPERVISOR_ROLES) if user else False
    is_store_manager = (user.role == 'store_manager') if user else False

    try:
        colleges = [c.to_dict() for c in College.query.order_by(College.id).all()]
    except Exception:
        db.session.rollback()
        colleges = []
    if not colleges:
        colleges = [{"id": 0, "name": "كلية الهندسة"}]

    try:
        updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    except Exception:
        db.session.rollback()
        updates = []

    # عدد المواد في كل كلية (لبطاقة الكلية «N مواد») — محسوب من بيانات الكتالوج نفسها.
    _college_subject_counts = {}
    for _cname, _cdata in data.items():
        _college_subject_counts[_cname] = sum(len(_t.get("subjects", [])) for _y in _cdata.get("years", {}).values() for _t in _y.get("terms", {}).values())
    for _c in colleges:
        _c["subject_count"] = _college_subject_counts.get(_c.get("name"), 0)

    # كتالوج JSON بترتيب الإدراج المحفوظ: فلتر tojson في Jinja يرتّب مفاتيح
    # القواميس أبجدياً (يونيكود) فيعيد السنوات 1، 3، 2 والكليات «الطب» قبل
    # «الهندسة» داخل CATALOG_DATA، رغم أن get_platform_data يرتّبها رقمياً —
    # فكان التنقّل داخل الصفحة (PJAX من CATALOG_DATA) يعرض 1، 3، 2 بينما
    # التحميل الكامل (Jinja من القاموس المرتب) يعرض 1، 2، 3. هذا الـ JSON
    # يحفظ ترتيب الإدراج الرقمي كما بناه get_platform_data، مع هروب آمن
    # لعلامات HTML كما يفعل tojson (بلا فرز للمفاتيح).
    try:
        catalog_json = json.dumps(data, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    except Exception:
        app.logger.exception("تعذر بناء catalog_json")
        catalog_json = '{}'

    view_context = {
        'catalog_error': catalog_error,
        'updates': updates,
        'is_admin': is_admin,
        'is_assistant': is_assistant,
        'is_supervisor': is_supervisor,
        'is_store_manager': is_store_manager,
        'user_role': user.role if user else '',
        'supervisor_college': (user.supervisor_college or '') if user else '',
        'supervisor_specialization': (user.supervisor_specialization or '') if user else '',
        'student_id': user.id if user else None,
        'student_name': user.name if user else None,
        'colleges': colleges,
        'data': data,
        'catalog_json': catalog_json,
        # ترتيب الكليات حسب id (الهندسة أولاً) لفرز القوائم في جافاسكربت؛
        # tojson يحفظ ترتيب القوائم كما هو (الفرز الأبجدي يطال مفاتيح القواميس فقط).
        'college_order': [c.get("name") for c in colleges],
        # خدمات VIP المغلقة حالياً + خدمات VIP التي يملك المستخدم منحاً عليها.
        'vip_closed_services': [k for k in VIP_SERVICE_KEY_LIST if _service_is_vip(k)],
        'my_vip_services': [g.scope_key for g in VipGrant.query.filter_by(student_id=user.id, scope_type='service').all() if _vip_grant_is_active(g)] if user else [],
        # سبب إنهاء جلسة (حظر/حذف) يُعرض مرة واحدة بعد إنهاء الجلسة.
        'terminated_reason': session.pop('terminated_reason', None),
    }

    college_name = (request.args.get('college') or '').strip()
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')
    chapter_name = request.args.get('chapter')

    if request.args.get('page') == 'services':
        return render_template('index.html', view='services_hub', **view_context)

    def _find_subject(sid):
        for c in data.values():
            for y in c["years"].values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == sid:
                            return s
        return None

    def _find_year_term(yname, tname=None):
        # أول كلية تحوي هذه السنة (والترم إن حدّد) — كلية الهندسة أولاً لأنها بُذرت أولاً
        for c in data.values():
            years = c["years"]
            if yname in years:
                if tname is None:
                    return c, years[yname]
                if tname in years[yname]["terms"]:
                    return c, years[yname]
        return None, None

    if subject_id and content_type and chapter_name:
        s = _find_subject(subject_id)
        if s and content_type in s["types"]:
            for c in s["types"][content_type]["chapters"]:
                if c["name"] == chapter_name:
                    q_query = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name)
                    if is_admin or is_assistant:
                        questions = q_query.order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                    else:
                        questions = q_query.filter_by(is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                    # فصل اختبار VIP مغلق: يُعرض صندوق القفل بدل زر البدء.
                    chapter_vip_locked = (content_type == 'quiz' and bool(c.get("vip_locked")))
                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, chapter_vip_locked=chapter_vip_locked, **view_context)

    if subject_id and content_type:
        s = _find_subject(subject_id)
        if s and content_type in s["types"]:
            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], **view_context)

    if subject_id:
        s = _find_subject(subject_id)
        if s:
            return render_template('index.html', view='select_type', subject=s, **view_context)

    if year and term:
        c, ydata = _find_year_term(year, term)
        if ydata:
            return render_template('index.html', view='subjects', subjects=ydata["terms"][term]["subjects"], y_id=year, college_name=c["name"], **view_context)

    if year:
        c, ydata = _find_year_term(year)
        if ydata:
            # فرز صريح عند العرض (حزام أمان فوق ترتيب get_platform_data):
            # مفاتيح القاموس وحدها لا تضمن الترتيب الرقمي 1، 2، 3.
            _terms_sorted = dict(sorted(ydata["terms"].items(), key=lambda kv: _term_order(kv[0])))
            return render_template('index.html', view='terms', terms=_terms_sorted, y_id=year, college_name=c["name"], **view_context)

    if college_name and college_name in data:
        # فرز صريح عند العرض (حزام أمان فوق ترتيب get_platform_data).
        _years_sorted = dict(sorted(data[college_name]["years"].items(), key=lambda kv: _year_order(kv[0])))
        return render_template('index.html', view='years', years=_years_sorted, college_name=college_name, **view_context)

    return render_template('index.html', view='colleges', **view_context)

# ==========================================
# Auth APIs (تسجيل الدخول والتسجيل)
# ==========================================

@app.route('/student/login', methods=['POST'])
def student_login():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({"status": "error", "message": "يرجى إدخال اسم المستخدم وكلمة المرور"}), 400

        student = Student.query.filter(db.func.lower(Student.username) == username.lower()).first()
        if not student:
            # حساب محذوف سابقاً: رسالة الحذف بدل رسالة عامة.
            deleted = DeletedAccount.query.filter(db.func.lower(DeletedAccount.username) == username.lower()).first()
            if deleted:
                return jsonify({"status": "error", "message": "تم حذف هذا الحساب لمخالفة سياسة الموقع"}), 401
            # رسالة عامة عمداً: لا نكشف إن كان اليوزر موجوداً أم لا.
            return jsonify({"status": "error", "message": "تعذر تسجيل الدخول بهذه البيانات"}), 401

        if not student.is_active:
            return jsonify({"status": "error", "message": "تم حظر هذا الحساب لمخالفة سياسة الموقع"}), 401

        if not student.check_password(password):
            return jsonify({"status":"error","message":"تعذر تسجيل الدخول بهذه البيانات"}), 401

        session.pop('terminated_reason', None)
        session.permanent = True
        session['student_id'] = student.id
        session['student_name'] = student.name
        session['is_admin'] = (student.role == 'admin')
        session['is_assistant'] = (student.role == 'assistant')

        profile = UserProfile.query.filter_by(student_id=student.id).first()
        if not profile:
            profile = UserProfile(student_id=student.id)
            db.session.add(profile)
            db.session.commit()

        return jsonify({
            "status": "success",
            "student": student.to_dict(),
            "role": student.role or 'student'
        })
    except Exception as e:
        app.logger.exception("student_login failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ بالسيرفر: {str(e)}"}), 500

@app.route('/student/register', methods=['POST'])
def student_register():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()
        name = str(data.get('name') or '').strip()

        if not username or not password or not name:
            return jsonify({"status": "error", "message": "جميع الحقول مطلوبة"}), 400

        if Student.query.filter(db.func.lower(Student.username) == username.lower()).first():
            return jsonify({"status": "error", "message": "اسم المستخدم مسجل مسبقاً"}), 400

        # إنشاء الطالب بدون تمرير auth_user_id لتفادي أي خطأ في النوع مع Supabase
        student = Student(username=username, name=name, role='student')
        student.set_password(password)
        db.session.add(student)
        db.session.flush()

        # إن كان اليوزر محذوفاً سابقاً (شاهد قبر) فالتسجيل الجديد يلغي أثر الحذف.
        DeletedAccount.query.filter(db.func.lower(DeletedAccount.username) == username.lower()).delete(synchronize_session=False)

        profile = UserProfile(student_id=student.id)
        db.session.add(profile)
        db.session.commit()

        return jsonify({"status": "success", "message": "تم التسجيل بنجاح"})
    except Exception as e:
        app.logger.exception("student_register failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إنشاء الحساب. حاول مرة أخرى."}), 500

@app.route('/student/logout', methods=['POST'])
def student_logout():
    session.clear()
    return jsonify({"status": "success"})

# ==========================================
# نظام إدارة المحتوى والمراجعة (Workflow)
# ==========================================

def _is_placeholder_question_title(text):
    """هل نص السؤال مجرد كلمة نائبة بدل السؤال الحقيقي؟
    الملفات المستوردة وضعت كلمة "اختبار" (بالباء) في question_text
    بدل نص السؤال، وقد تظهر أيضاً بصيغة "اختيار" (بالياء). نقارن
    الحروف فقط بعد توحيد الألف والياء والكاف وحذف الحروف غير الأبجدية،
    لتجاهل الحروف غير المرئية التي قد تحملها البيانات المستوردة:
    الألف المهموزة \u0623 والمكسورة \u0625 والممدودة \u0622 وألف
    الوصلة \u0671 تصبح ألفاً عادية \u0627، والياء الفارسية \u06cc
    والألف المقصورة \u0649 تصبحان ياءً عربية \u064a، والكاف الفارسية
    \u06a9 تصبح كافاً عربية \u0643."""
    t = (text or "").strip()
    for _alef in ("\u0623", "\u0625", "\u0622", "\u0671"):
        t = t.replace(_alef, "\u0627")
    t = t.replace("\u06cc", "\u064a").replace("\u0649", "\u064a").replace("\u06a9", "\u0643")
    t = "".join(ch for ch in t if ch.isalpha())
    return t in ("اختبار", "اختيار")

def _safe_str(value, default=""):
    """نص آمن: يعيد القيمة الافتراضية عند None، وإلا النص بعد إزالة المسافات."""
    if value is None:
        return default
    return str(value).strip()

def _is_truthy(value):
    """هل قيمة قادمة من JSON/نموذج تعني True؟ (true/1/on/yes)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('true', '1', 'on', 'yes', 'y')

# القيم القياسية للسنة والترم (تطبيع الإدخال):
# أي صيغة مقبولة (رقم أو اسم مختصر أو كامل) تُطبع إلى القيمة القياسية عند
# إنشاء مادة أو إضافة/تعديل محتوى أو استيراد أسئلة، حتى لا تنشأ مواد مخفية
# عن الصفحة الرئيسية أو بطاقات سنة مكررة بسبب اختلاف صياغة النص الحر.
# القيم غير القابلة للتطبيع تُرفض برسالة 400 تذكر القيم المقبولة.
# الاستثناء: إضافة مادة من الواجهة تقبل سنة مخصصة كما كُتبت
# (مثل «سنة تحضيرية» أو «امتحان قبول»)؛ الاستيراد وإضافة/تعديل
# المحتوى يبقيان صارمين.
CANONICAL_YEARS = {
    '1': 'السنة الأولى', 'الأولى': 'السنة الأولى', 'السنة الأولى': 'السنة الأولى',
    'الاولى': 'السنة الأولى', 'السنة الاولى': 'السنة الأولى', 'السنة الاولئ': 'السنة الأولى',
    '2': 'السنة الثانية', 'الثانية': 'السنة الثانية', 'السنة الثانية': 'السنة الثانية',
    '3': 'السنة الثالثة', 'الثالثة': 'السنة الثالثة', 'السنة الثالثة': 'السنة الثالثة',
    '4': 'السنة الرابعة', 'الرابعة': 'السنة الرابعة', 'السنة الرابعة': 'السنة الرابعة',
    '5': 'السنة الخامسة', 'الخامسة': 'السنة الخامسة', 'السنة الخامسة': 'السنة الخامسة',
    '6': 'السنة السادسة', 'السادسة': 'السنة السادسة', 'السنة السادسة': 'السنة السادسة',
}
CANONICAL_TERMS = {
    '1': 'الترم الأول', 'الأول': 'الترم الأول', 'الترم الأول': 'الترم الأول', 'الفصل الأول': 'الترم الأول',
    '2': 'الترم الثاني', 'الثاني': 'الترم الثاني', 'الترم الثاني': 'الترم الثاني', 'الفصل الثاني': 'الترم الثاني',
}
_YEAR_VALUES_TEXT = '، '.join(['السنة الأولى', 'السنة الثانية', 'السنة الثالثة', 'السنة الرابعة', 'السنة الخامسة', 'السنة السادسة'])
_TERM_VALUES_TEXT = 'الترم الأول، الترم الثاني'

def _normalize_year(value):
    """تطبيع سنة دراسية إلى قيمتها القياسية، أو None إن لم تُطابق أي صيغة مقبولة."""
    return CANONICAL_YEARS.get(_safe_str(value))

def _normalize_term(value):
    """تطبيع فصل دراسي إلى قيمته القياسية، أو None إن لم تُطابق أي صيغة مقبولة."""
    return CANONICAL_TERMS.get(_safe_str(value))

@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    try:
        user = get_current_user()
        data = request.get_json(silent=True) or request.form or {}

        status = 'approved' if user.role == 'admin' else 'pending_review'

        title = _safe_str(data.get('title'))
        content_type = _safe_str(data.get('content_type'), 'videos')

        # Flow B: المادة تُحدَّد من القائمة المنسدلة (subject_id) وتُؤخذ
        # الكلية/السنة/الترم من صف المادة نفسه.
        subject = None
        subject_id_raw = data.get('subject_id')
        if subject_id_raw not in (None, '', 0, '0'):
            try:
                subject = Subject.query.get(int(subject_id_raw))
            except (TypeError, ValueError):
                subject = None
            if not subject:
                return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بالإضافة إلى مادة خارج نطاقك"}), 403
            college_value = subject.college
            year = subject.year
            term = subject.term
            subject_name = subject.name
        else:
            # مسار التوافق: بلا subject_id يُقبل الاسم فقط إن طابق مادة موجودة.
            # السنة والترم يُطبَّعان إلى القيم القياسية حتى يطابقا صف المادة.
            _raw_year = _safe_str(data.get('year'))
            _raw_term = _safe_str(data.get('term'))
            year = _normalize_year(_raw_year) if _raw_year else None
            term = _normalize_term(_raw_term) if _raw_term else None
            if _raw_year and not year:
                return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — القيم المقبولة: " + _YEAR_VALUES_TEXT}), 400
            if _raw_term and not term:
                return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
            subject_name = _safe_str(data.get('subject_name'))
            # نطاق المشرف: مشرف الكلية/التخصص لا يضيف إلا ضمن نطاقه،
            # والمشرف الأعلى يختار الكلية من النموذج (الافتراضي: كلية الهندسة).
            if user.role == 'college_supervisor':
                college_value = user.supervisor_college or 'كلية الهندسة'
            elif user.role == 'specialization_supervisor':
                college_value = user.supervisor_college or 'كلية الهندسة'
                subject_name = user.supervisor_specialization or subject_name
            else:
                college_value = _safe_str(data.get('college')) or 'كلية الهندسة'
            if not College.query.filter_by(name=college_value).first():
                db.session.add(College(name=college_value))
            if not year or not term or not subject_name:
                return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
            subject = Subject.query.filter_by(college=college_value, year=year, term=term, name=subject_name).first()
            if not subject:
                return jsonify({"status": "error", "message": "اختر المادة أولاً — لا توجد مادة بهذا الاسم في الكلية والسنة والترم"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بالإضافة إلى مادة خارج نطاقك"}), 403

        if not title:
            return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
        if content_type not in ('videos', 'files', 'quiz'):
            return jsonify({"status": "error", "message": "نوع المحتوى غير صالح (يجب أن يكون videos أو files أو quiz)"}), 400

        lesson = CustomLesson(
            year=year,
            term=term,
            college=college_value,
            subject_name=subject_name,
            subject_id=subject.id,
            chapter_name=_safe_str(data.get('chapter_name'), 'عام') or 'عام',
            content_type=content_type,
            title=title,
            url=_safe_str(data.get('url')),
            download_url=_safe_str(data.get('download_url')),
            notes=_safe_str(data.get('notes')),
            status=status,
            is_vip=_is_truthy(data.get('is_vip')),
            created_by=user.id
        )
        db.session.add(lesson)
        db.session.commit()

        msg = "تم نشر المحتوى بنجاح!" if status == 'approved' else "تم رفع المحتوى وإرساله لطابور المراجعة."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        app.logger.exception("فشل إضافة محتوى")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@can_manage_content
def get_custom_content():
    user = get_current_user()
    try:
        if user.role == 'admin':
            lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
        elif user.role in SUPERVISOR_ROLES:
            lessons = _scope_query(CustomLesson.query, user, CustomLesson).order_by(CustomLesson.created_at.desc()).all()
        else:
            lessons = CustomLesson.query.filter((CustomLesson.created_by == user.id) | (CustomLesson.status == 'approved')).order_by(CustomLesson.created_at.desc()).all()
        return jsonify([l.to_dict() for l in lessons])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/edit-content/<int:content_id>', methods=['POST'])
@can_manage_content
def edit_custom_content(content_id):
    try:
        user = get_current_user()
        item = CustomLesson.query.get_or_404(content_id)

        if user.role == 'admin':
            pass
        elif user.role in SUPERVISOR_ROLES:
            if not _in_scope(user, item.college, item.subject_name, item.subject_id):
                return jsonify({"status": "error", "message": "غير مصرح لك بتعديل محتوى خارج نطاقك"}), 403
        elif item.created_by != user.id:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل محتوى الآخرين"}), 403

        data = request.get_json(silent=True) or request.form or {}
        # نقل الدرس إلى مادة أخرى عبر القائمة المنسدلة (subject_id):
        # الكلية/السنة/الترم/الاسم تُؤخذ من صف المادة، وهوية الدرس لا تتغير
        # فتبقى مراجع التقدّم والمفضلة والمهام سليمة.
        _move_subject_id = data.get('subject_id')
        if _move_subject_id not in (None, '', 0, '0'):
            try:
                _target = Subject.query.get(int(_move_subject_id))
            except (TypeError, ValueError):
                _target = None
            if not _target:
                return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, _target.college, _target.name, _target.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بنقل المحتوى إلى مادة خارج نطاقك"}), 403
            item.subject_id = _target.id
            item.subject_name = _target.name
            item.year = _target.year
            item.term = _target.term
            item.college = _target.college
        else:
            # قيمة سنة/ترم مرسلة تُطبَّع إلى القيم القياسية؛ قيمة غير قابلة
            # للتطبيع تُرفض بدل كتابة نص حر يفك ارتباط الدرس بمادته.
            _raw_year = _safe_str(data.get('year'))
            _raw_term = _safe_str(data.get('term'))
            if _raw_year:
                _ny = _normalize_year(_raw_year)
                if not _ny:
                    return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — القيم المقبولة: " + _YEAR_VALUES_TEXT}), 400
                item.year = _ny
            if _raw_term:
                _nt = _normalize_term(_raw_term)
                if not _nt:
                    return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
                item.term = _nt
            item.college = _safe_str(data.get('college'), item.college)
            item.subject_name = _safe_str(data.get('subject_name'), item.subject_name)
        # المشرف لا ينقل المحتوى خارج نطاقه
        if user.role == 'college_supervisor':
            item.college = user.supervisor_college or item.college
        elif user.role == 'specialization_supervisor':
            item.college = user.supervisor_college or item.college
            item.subject_name = user.supervisor_specialization or item.subject_name
        # بلا نقل عبر القائمة: أعد مزامنة subject_id مع (الكلية، السنة، الترم، الاسم)
        if _move_subject_id in (None, '', 0, '0'):
            _synced = Subject.query.filter_by(college=item.college, year=item.year, term=item.term, name=item.subject_name).first()
            item.subject_id = _synced.id if _synced else None
        item.chapter_name = _safe_str(data.get('chapter_name'), item.chapter_name)
        item.content_type = _safe_str(data.get('content_type'), item.content_type)
        item.title = _safe_str(data.get('title'), item.title)
        item.url = _safe_str(data.get('url'), item.url)
        item.download_url = _safe_str(data.get('download_url'), item.download_url)
        item.notes = _safe_str(data.get('notes'), item.notes)
        # علم VIP قابل للتبديل في أي وقت (VIP ⇄ مجاني) من نموذج التعديل نفسه.
        if 'is_vip' in data:
            item.is_vip = _is_truthy(data.get('is_vip'))

        if not item.year or not item.term or not item.subject_name or not item.title:
            return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
        if item.content_type not in ('videos', 'files', 'quiz'):
            return jsonify({"status": "error", "message": "نوع المحتوى غير صالح (يجب أن يكون videos أو files أو quiz)"}), 400

        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث المحتوى بنجاح"})
    except Exception as e:
        app.logger.exception("فشل تعديل محتوى: content_id=%s", content_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-content/<int:content_id>', methods=['POST'])
@can_manage_content
def delete_custom_content(content_id):
    try:
        user = get_current_user()
        item = CustomLesson.query.get_or_404(content_id)
        if user.role != 'admin' and not _in_scope(user, item.college, item.subject_name):
            return jsonify({"status": "error", "message": "غير مصرح لك بحذف محتوى خارج نطاقك"}), 403
        # حذف المرتبطات أولاً لتفادي مخالفة المفتاح الأجنبي، وفصل المهام عن الدرس المحذوف
        Favorite.query.filter_by(lesson_id=content_id).delete(synchronize_session=False)
        Progress.query.filter_by(lesson_id=content_id).delete(synchronize_session=False)
        UnifiedTask.query.filter_by(lesson_id=content_id).update({"lesson_id": None}, synchronize_session=False)
        db.session.delete(item)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/review-queue', methods=['GET'])
@admin_or_supervisor_required
def get_review_queue():
    try:
        user = get_current_user()
        lessons = _scope_query(CustomLesson.query.filter_by(status='pending_review'), user, CustomLesson).all()
        quizzes = _scope_query(QuizQuestion.query.filter_by(status='pending_review'), user, QuizQuestion).all()
        return jsonify({
            "lessons": [l.to_dict() for l in lessons],
            "quizzes": [q.to_dict() for q in quizzes]
        })
    except Exception:
        db.session.rollback()
        return jsonify({"lessons": [], "quizzes": []})

@app.route('/admin/approve-content/<string:content_type>/<int:item_id>', methods=['POST'])
@admin_required
def approve_content(content_type, item_id):
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'approve')
        new_status = 'approved' if action == 'approve' else 'rejected'

        if content_type == 'lesson':
            item = CustomLesson.query.get_or_404(item_id)
        else:
            item = QuizQuestion.query.get_or_404(item_id)

        item.status = new_status
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم {'الاعتماد' if action == 'approve' else 'الرفض'}"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# الكليات والمشرفون (المشرف الأعلى فقط)
# ==========================================

@app.route('/admin/colleges', methods=['GET'])
@admin_required
def get_colleges():
    try:
        return jsonify([c.to_dict() for c in College.query.order_by(College.id).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-college', methods=['POST'])
@admin_required
def add_college():
    try:
        data = request.get_json(silent=True) or request.form or {}
        name = _safe_str(data.get('name'))
        if not name:
            return jsonify({"status": "error", "message": "اسم الكلية مطلوب"}), 400
        if College.query.filter_by(name=name).first():
            return jsonify({"status": "error", "message": "هذه الكلية موجودة بالفعل"}), 400
        db.session.add(College(name=name))
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إضافة الكلية بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-college', methods=['POST'])
@admin_required
def remove_college():
    try:
        data = request.get_json(silent=True) or request.form or {}
        college_id = data.get('id')
        college = None
        if college_id:
            try:
                college = College.query.get(int(college_id))
            except (TypeError, ValueError):
                college = None
        if not college and _safe_str(data.get('name')):
            college = College.query.filter_by(name=_safe_str(data.get('name'))).first()
        if not college:
            return jsonify({"status": "error", "message": "الكلية غير موجودة"}), 404
        name = college.name
        if CustomLesson.query.filter_by(college=name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية فيها مقررات — انقل المقررات أو احذفها أولاً"}), 400
        if QuizQuestion.query.filter_by(college=name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية فيها أسئلة اختبار — احذف الأسئلة أولاً"}), 400
        if Student.query.filter(Student.role.in_(list(SUPERVISOR_ROLES)), Student.supervisor_college == name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية لديها مشرفون — أزل المشرفين أولاً"}), 400
        db.session.delete(college)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف الكلية بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# المواد ككيانات (Flow A: إضافة مادة مستقلة)
# ==========================================

def _subject_lesson_count(subj):
    """عدد الدروس المرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)."""
    n = CustomLesson.query.filter_by(subject_id=subj.id).count()
    n += CustomLesson.query.filter(
        CustomLesson.subject_id == None,
        CustomLesson.college == subj.college,
        CustomLesson.year == subj.year,
        CustomLesson.term == subj.term,
        CustomLesson.subject_name == subj.name).count()
    return n

def _subject_question_count(subj):
    """عدد أسئلة الاختبار المرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)."""
    n = QuizQuestion.query.filter_by(subject_id=subj.id).count()
    n += QuizQuestion.query.filter(
        QuizQuestion.subject_id == None,
        QuizQuestion.college == subj.college,
        QuizQuestion.year == subj.year,
        QuizQuestion.term == subj.term,
        QuizQuestion.subject_name == subj.name).count()
    return n

def _subject_lesson_titles(subj, limit=3):
    """عناوين الدروس المرتبطة بالمادة (حتى limit) لعرضها في رسائل الرفض،
    حتى يرى المستخدم ما الذي يمنع الحذف بدل «فيها دروس» فقط."""
    rows = CustomLesson.query.filter_by(subject_id=subj.id).order_by(CustomLesson.id).limit(limit).all()
    if len(rows) < limit:
        rows += CustomLesson.query.filter(
            CustomLesson.subject_id == None,
            CustomLesson.college == subj.college,
            CustomLesson.year == subj.year,
            CustomLesson.term == subj.term,
            CustomLesson.subject_name == subj.name).order_by(CustomLesson.id).limit(limit - len(rows)).all()
    titles = []
    for r in rows:
        _t = (r.title or '').strip()
        _ch = (r.chapter_name or '').strip()
        titles.append((_t + (' — ' + _ch if _ch else '')) or 'درس بدون عنوان')
    return titles

def _subject_has_lessons(subj):
    """هل توجد دروس مرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)؟"""
    return _subject_lesson_count(subj) > 0

def _subject_has_questions(subj):
    """هل توجد أسئلة اختبار مرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)؟"""
    return _subject_question_count(subj) > 0

@app.route('/admin/subjects', methods=['GET'])
@admin_or_supervisor_required
def get_subjects():
    user = get_current_user()
    try:
        query = Subject.query
        if user.role == 'college_supervisor':
            query = query.filter_by(college=(user.supervisor_college or ''))
        elif user.role == 'specialization_supervisor':
            if getattr(user, 'supervisor_subject_id', None):
                query = query.filter_by(id=user.supervisor_subject_id)
            else:
                query = query.filter_by(college=(user.supervisor_college or ''),
                                        name=(user.supervisor_specialization or ''))
        # ترتيب ثابت رقمياً للسنة/الترم: ORDER BY النصي في SQL يضع
        # «السنة الثالثة» قبل «السنة الثانية» فيعرض 1، 3، 2.
        rows = query.all()
        rows.sort(key=lambda s: ((s.college or ""), _year_order(s.year), _term_order(s.term), (s.name or "")))
        return jsonify([s.to_dict() for s in rows])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-subject', methods=['POST'])
@admin_or_supervisor_required
def add_subject():
    try:
        user = get_current_user()
        if user.role == 'specialization_supervisor':
            return jsonify({"status": "error", "message": "مشرف التخصص لا يمكنه إضافة مواد — المادة الخاصة بك موجودة بالفعل"}), 403
        data = request.get_json(silent=True) or request.form or {}
        name = _safe_str(data.get('name'))
        raw_year = _safe_str(data.get('year'))
        raw_term = _safe_str(data.get('term'))
        if user.role == 'college_supervisor':
            college = user.supervisor_college or 'كلية الهندسة'
        else:
            college = _safe_str(data.get('college')) or 'كلية الهندسة'
        if not name or not raw_year or not raw_term:
            return jsonify({"status": "error", "message": "اسم المادة والسنة والترم حقول مطلوبة"}), 400
        # السنة تُطبَّع إلى قيمتها القياسية إن طابقت صيغة معروفة (رقم أو
        # مختصر أو كامل)؛ وإلا تُقبل كما كُتبت كسنة مخصصة (مثل «سنة تحضيرية»
        # أو «امتحان قبول») طالما كانت نصاً غير فارغ ضمن طول العمود (100).
        # الترم يبقى مقيّداً بالقيمتين القياسيتين فقط.
        year = _normalize_year(raw_year)
        if not year:
            year = raw_year.strip()
            if not year or len(year) > 100:
                return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — اختر سنة من القائمة أو اكتب سنة مخصصة (100 حرف كحد أقصى)"}), 400
        term = _normalize_term(raw_term)
        if not term:
            return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
        if not College.query.filter_by(name=college).first():
            db.session.add(College(name=college))
        if Subject.query.filter_by(college=college, year=year, term=term, name=name).first():
            return jsonify({"status": "error", "message": "هذه المادة موجودة بالفعل في نفس الكلية والسنة والترم"}), 400
        subj = Subject(name=name, college=college, year=year, term=term, created_by=user.id)
        db.session.add(subj)
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إضافة المادة بنجاح", "subject": subj.to_dict()})
    except Exception as e:
        app.logger.exception("فشل إضافة مادة")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/edit-subject/<int:subject_id>', methods=['POST'])
@admin_or_supervisor_required
def edit_subject(subject_id):
    """تعديل مادة: إعادة التسمية و/أو النقل إلى كلية/سنة/ترم آخر.
    النقل يحدّث المادة وكل صفوفها المرتبطة (دروس + أسئلة) دفعة واحدة
    في نفس المعاملة — الحقول المنسوخة (college/year/term/subject_name)
    على الصفوف المرتبطة بـ subject_id وكذلك الصفوف القديمة بلا
    subject_id المطابقة للاسم القديم — حتى لا يظهر المحتوى تحت
    السنة/الترم القديمين. subject_id نفسه لا يتغير، فلا تتأثر
    Progress/Favorites. الصلاحيات: المشرف الأعلى لأي كلية، مشرف الكلية
    داخل كليته فقط (المصدر والهدف)، مشرف التخصص لا يعدّل المواد."""
    try:
        user = get_current_user()
        subj = Subject.query.get_or_404(subject_id)
        if user.role == 'specialization_supervisor':
            return jsonify({"status": "error", "message": "مشرف التخصص لا يمكنه تعديل المواد"}), 403
        if user.role == 'college_supervisor' and (user.supervisor_college or '') != (subj.college or ''):
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل مادة خارج كليتك"}), 403
        data = request.get_json(silent=True) or request.form or {}
        new_name = _safe_str(data.get('name'))
        if not new_name:
            return jsonify({"status": "error", "message": "اسم المادة مطلوب"}), 400
        # الكلية الهدف: إن لم تُرسل تُبقى الحالية. مشرف الكلية لا ينقل
        # خارج كليته — طلب كلية أخرى يُرفض 403 برسالة واضحة.
        new_college = _safe_str(data.get('college')) or subj.college
        if user.role == 'college_supervisor':
            if new_college != (user.supervisor_college or ''):
                return jsonify({"status": "error", "message": "غير مصرح لك بنقل مادة إلى كلية أخرى — النقل داخل كليتك فقط"}), 403
            new_college = user.supervisor_college or subj.college
        # السنة: تُطبَّع إن طابقت صيغة معروفة، وإلا تُقبل كما كُتبت كسنة
        # مخصصة (مثل add-subject) طالما كانت ضمن طول العمود (100).
        raw_year = _safe_str(data.get('year'))
        if raw_year:
            new_year = _normalize_year(raw_year)
            if not new_year:
                new_year = raw_year.strip()
                if not new_year or len(new_year) > 100:
                    return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — اختر سنة من القائمة أو اكتب سنة مخصصة (100 حرف كحد أقصى)"}), 400
        else:
            new_year = subj.year
        # الترم: قياسي فقط.
        raw_term = _safe_str(data.get('term'))
        if raw_term:
            new_term = _normalize_term(raw_term)
            if not new_term:
                return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
        else:
            new_term = subj.term
        if (new_name, new_college, new_year, new_term) == (subj.name, subj.college, subj.year, subj.term):
            return jsonify({"status": "success", "message": "تم تحديث المادة بنجاح", "subject": subj.to_dict()})
        dup = Subject.query.filter_by(college=new_college, year=new_year, term=new_term, name=new_name).first()
        if dup and dup.id != subj.id:
            return jsonify({"status": "error", "message": "يوجد مادة بهذا الاسم بالفعل في %s — %s — %s" % (new_college, new_year, new_term)}), 400
        old = (subj.college, subj.year, subj.term, subj.name)
        subj.name = new_name
        subj.college = new_college
        subj.year = new_year
        subj.term = new_term
        _moved = {"subject_name": new_name, "college": new_college, "year": new_year, "term": new_term}
        CustomLesson.query.filter_by(subject_id=subj.id).update(_moved, synchronize_session=False)
        CustomLesson.query.filter(
            CustomLesson.subject_id == None,
            CustomLesson.college == old[0], CustomLesson.year == old[1],
            CustomLesson.term == old[2], CustomLesson.subject_name == old[3]).update(_moved, synchronize_session=False)
        QuizQuestion.query.filter_by(subject_id=subj.id).update(_moved, synchronize_session=False)
        QuizQuestion.query.filter(
            QuizQuestion.subject_id == None,
            QuizQuestion.college == old[0], QuizQuestion.year == old[1],
            QuizQuestion.term == old[2], QuizQuestion.subject_name == old[3]).update(_moved, synchronize_session=False)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث المادة بنجاح", "subject": subj.to_dict()})
    except Exception as e:
        app.logger.exception("فشل تعديل مادة: subject_id=%s", subject_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-subject', methods=['POST'])
@admin_required
def remove_subject():
    try:
        data = request.get_json(silent=True) or request.form or {}
        subject_id = data.get('id')
        subj = None
        if subject_id:
            try:
                subj = Subject.query.get(int(subject_id))
            except (TypeError, ValueError):
                subj = None
        if not subj:
            return jsonify({"status": "error", "message": "المادة غير موجودة"}), 404
        if _subject_has_lessons(subj):
            _titles = _subject_lesson_titles(subj, 3)
            _extra = (' (مثال: ' + '، '.join(_titles) + ')') if _titles else ''
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة فيها دروس — انقل الدروس أو احذفها أولاً" + _extra}), 400
        if _subject_has_questions(subj):
            _nq = _subject_question_count(subj)
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة فيها أسئلة اختبار — احذف الأسئلة أولاً (عددها: %d)" % _nq}), 400
        if Student.query.filter(Student.role == 'specialization_supervisor', Student.supervisor_subject_id == subj.id).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة لديها مشرف تخصص — أزل المشرف أولاً"}), 400
        if Student.query.filter(Student.role == 'specialization_supervisor', Student.supervisor_subject_id == None, Student.supervisor_college == subj.college, Student.supervisor_specialization == subj.name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة لديها مشرف تخصص — أزل المشرف أولاً"}), 400
        db.session.delete(subj)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف المادة بنجاح"})
    except Exception as e:
        app.logger.exception("فشل حذف مادة")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/quiz-questions', methods=['GET'])
@admin_or_supervisor_required
def get_quiz_questions():
    """قائمة أسئلة الاختبار المستوردة (لإدارتها وحذفها): المشرف الأعلى يرى
    الكل، ومشرف الكلية يرى كليته فقط، ومشرف التخصص يرى مادته فقط.
    ملاحظة: الاسم مختلف عن /admin/questions الموجود (استفسارات الطلاب)."""
    try:
        user = get_current_user()
        subject_id = request.args.get('subject_id', type=int)
        query = QuizQuestion.query
        if subject_id:
            query = query.filter(QuizQuestion.subject_id == subject_id)
        query = _scope_query(query, user, QuizQuestion)
        qs = query.order_by(QuizQuestion.subject_name, QuizQuestion.chapter_name, QuizQuestion.id).all()
        return jsonify([q.to_dict() for q in qs])
    except Exception:
        app.logger.exception("فشل جلب أسئلة الاختبار")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/delete-quiz-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_quiz_question(question_id):
    """حذف سؤال اختبار معتمد — المشرف الأعلى فقط (الحذف النهائي له وحده).
    بدونه تبقى المادة التي فيها أسئلة مستوردة غير قابلة للحذف من الواجهة."""
    try:
        q = QuizQuestion.query.get(question_id)
        if not q:
            return jsonify({"status": "error", "message": "السؤال غير موجود"}), 404
        db.session.delete(q)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف السؤال بنجاح"})
    except Exception as e:
        app.logger.exception("فشل حذف سؤال اختبار: question_id=%s", question_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# دليل الطالب — آراء الطلاب عن الدكاترة
# ==========================================

@app.route('/api/doctor-opinions', methods=['GET'])
def list_doctor_opinions():
    """الآراء المعتمدة فقط (قراءة مفتوحة للجميع): فلتر اختياري بالكلية،
    وبحث جزئي باسم الدكتور عبر q. تعيد العدّادات + صوت الطالب الحالي."""
    try:
        college = _safe_str(request.args.get('college'))
        q = _safe_str(request.args.get('q'))
        query = DoctorOpinion.query.filter_by(status='approved')
        if college:
            query = query.filter_by(college=college)
        if q:
            query = query.filter(DoctorOpinion.doctor_name.ilike(f"%{q}%"))
        viewer_id = session.get('student_id')
        opinions = query.order_by(DoctorOpinion.created_at.desc()).all()
        return jsonify([o.to_dict(viewer_id=viewer_id) for o in opinions])
    except Exception:
        app.logger.exception("فشل جلب آراء الدكاترة")
        db.session.rollback()
        return jsonify([])

@app.route('/api/doctor-opinions', methods=['POST'])
@student_required
def add_doctor_opinion():
    """إضافة رأي جديد: كل الحقول مطلوبة، والحالة pending حتى يوافق
    المشرف الأعلى — لا يظهر الرأي للطلاب قبل الاعتماد."""
    try:
        student_id = session.get('student_id')
        data = request.get_json(silent=True) or {}
        doctor_name = _safe_str(data.get('doctor_name'))
        subject = _safe_str(data.get('subject'))
        college = _safe_str(data.get('college'))
        comment = _safe_str(data.get('comment'))
        anonymous = bool(data.get('anonymous'))
        if not doctor_name or not subject or not college or not comment:
            return jsonify({"status": "error", "message": "كل الحقول مطلوبة: اسم الدكتور والمادة والكلية والتعليق"}), 400
        if len(doctor_name) > 150 or len(subject) > 150 or len(college) > 150:
            return jsonify({"status": "error", "message": "اسم الدكتور أو المادة أو الكلية أطول من المسموح (150 حرفاً)"}), 400
        op = DoctorOpinion(student_id=student_id, doctor_name=doctor_name, subject=subject,
                           college=college, comment=comment, anonymous=anonymous, status='pending')
        db.session.add(op)
        db.session.commit()
        return jsonify({"status": "success",
                        "message": "تم إرسال رأيك للمراجعة، وسيظهر بعد موافقة المشرف الأعلى",
                        "opinion": op.to_dict(viewer_id=student_id)})
    except Exception:
        app.logger.exception("فشل إضافة رأي عن دكتور")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إرسال رأيك. حاول مجدداً."}), 500

@app.route('/api/doctor-opinions/<int:opinion_id>/vote', methods=['POST'])
@student_required
def vote_doctor_opinion(opinion_id):
    """التصويت على رأي معتمد: 1 = لايك، -1 = ديسلايك. صوت واحد لكل طالب؛
    الضغط على نفس الصوت مرة ثانية يلغيه، والضغط على الصوت الآخر يبدّله."""
    try:
        student_id = session.get('student_id')
        data = request.get_json(silent=True) or {}
        try:
            value = int(data.get('value'))
        except (TypeError, ValueError):
            value = 0
        if value not in (1, -1):
            return jsonify({"status": "error", "message": "قيمة التصويت غير صالحة"}), 400
        op = DoctorOpinion.query.get_or_404(opinion_id)
        if op.status != 'approved':
            return jsonify({"status": "error", "message": "التصويت متاح فقط على الآراء المعتمدة"}), 400
        existing = DoctorOpinionVote.query.filter_by(opinion_id=opinion_id, student_id=student_id).first()
        if existing and existing.value == value:
            db.session.delete(existing)
        elif existing:
            existing.value = value
        else:
            db.session.add(DoctorOpinionVote(opinion_id=opinion_id, student_id=student_id, value=value))
        db.session.commit()
        return jsonify({"status": "success", "opinion": op.to_dict(viewer_id=student_id)})
    except Exception:
        app.logger.exception("فشل التصويت على رأي: opinion_id=%s", opinion_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حفظ تصويتك. حاول مجدداً."}), 500

@app.route('/admin/doctor-opinions', methods=['GET'])
@admin_required
def get_doctor_opinions_admin():
    """إدارة آراء الدكاترة (المشرف الأعلى فقط): المعلّقة أولاً، مع فلتر
    ?status= اختياري (pending/approved/rejected)."""
    try:
        user = get_current_user()
        status = _safe_str(request.args.get('status'))
        query = DoctorOpinion.query
        if status in ('pending', 'approved', 'rejected'):
            query = query.filter_by(status=status)
        opinions = query.order_by(
            db.case((DoctorOpinion.status == 'pending', 0), else_=1),
            DoctorOpinion.created_at.desc()).all()
        return jsonify([o.to_dict(viewer_id=user.id, include_real_author=True) for o in opinions])
    except Exception:
        app.logger.exception("فشل جلب آراء الدكاترة للإدارة")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/doctor-opinions/<int:opinion_id>/approve', methods=['POST'])
@admin_required
def approve_doctor_opinion(opinion_id):
    try:
        op = DoctorOpinion.query.get_or_404(opinion_id)
        op.status = 'approved'
        db.session.commit()
        return jsonify({"status": "success", "message": "تم اعتماد الرأي وسيظهر للطلاب"})
    except Exception:
        app.logger.exception("فشل اعتماد رأي: opinion_id=%s", opinion_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر اعتماد الرأي. حاول مجدداً."}), 500

@app.route('/admin/doctor-opinions/<int:opinion_id>/reject', methods=['POST'])
@admin_required
def reject_doctor_opinion(opinion_id):
    try:
        op = DoctorOpinion.query.get_or_404(opinion_id)
        op.status = 'rejected'
        db.session.commit()
        return jsonify({"status": "success", "message": "تم رفض الرأي ولن يظهر للطلاب"})
    except Exception:
        app.logger.exception("فشل رفض رأي: opinion_id=%s", opinion_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر رفض الرأي. حاول مجدداً."}), 500

@app.route('/admin/doctor-opinions/<int:opinion_id>/delete', methods=['POST'])
@admin_required
def delete_doctor_opinion(opinion_id):
    """حذف رأي نهائياً مع كل أصواته — المشرف الأعلى فقط."""
    try:
        op = DoctorOpinion.query.get_or_404(opinion_id)
        DoctorOpinionVote.query.filter_by(opinion_id=opinion_id).delete(synchronize_session=False)
        db.session.delete(op)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف الرأي"})
    except Exception:
        app.logger.exception("فشل حذف رأي: opinion_id=%s", opinion_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حذف الرأي. حاول مجدداً."}), 500

@app.route('/admin/supervisors', methods=['GET'])
@admin_required
def get_supervisors():
    try:
        sups = Student.query.filter(Student.role.in_(list(SUPERVISOR_ROLES))).order_by(Student.created_at.desc()).all()
        return jsonify([s.to_dict() for s in sups])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-supervisor', methods=['POST'])
@admin_required
def add_supervisor():
    username = role = college = specialization = ""
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = _safe_str(data.get('username'))
        role = _safe_str(data.get('role'))
        college = _safe_str(data.get('college'))
        specialization = _safe_str(data.get('specialization'))
        if not username or role not in SUPERVISOR_ROLES:
            return jsonify({"status": "error", "message": "اسم المستخدم والدور حقول مطلوبة"}), 400
        if not college:
            return jsonify({"status": "error", "message": "الكلية مطلوبة"}), 400
        if role == 'specialization_supervisor' and not specialization:
            return jsonify({"status": "error", "message": "التخصص مطلوب لمشرف التخصص"}), 400
        if not College.query.filter_by(name=college).first():
            return jsonify({"status": "error", "message": "الكلية غير موجودة — أضفها أولاً"}), 400
        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "لا يوجد حساب بهذا الاسم"}), 404
        if student.role == 'admin':
            return jsonify({"status": "error", "message": "هذا الحساب مشرف أعلى بالفعل"}), 400
        subject = None
        if role == 'specialization_supervisor':
            subject_id = data.get('subject_id')
            if subject_id:
                try:
                    subject = Subject.query.get(int(subject_id))
                except (TypeError, ValueError):
                    subject = None
                if not subject:
                    return jsonify({"status": "error", "message": "المادة المختارة غير موجودة"}), 400
                if subject.college != college:
                    return jsonify({"status": "error", "message": "المادة المختارة ليست في الكلية المحددة"}), 400
                specialization = subject.name
                college = subject.college
            elif specialization:
                # توافق: حلّ الاسم إلى مادة محددة إن وُجدت مطابقة واحدة
                matches = Subject.query.filter_by(college=college, name=specialization).all()
                if len(matches) == 1:
                    subject = matches[0]
        student.role = role
        student.supervisor_college = college
        student.supervisor_specialization = specialization if role == 'specialization_supervisor' else None
        student.supervisor_subject_id = subject.id if (role == 'specialization_supervisor' and subject) else None
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تعيين المشرف بنجاح", "supervisor": student.to_dict()})
    except Exception as e:
        app.logger.exception("فشل تعيين مشرف: username=%s role=%s college=%s", username, role, college)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-supervisor', methods=['POST'])
@admin_required
def remove_supervisor():
    # جولة 5.4: حراسة — إزالة الإشراف كانت بلا أي حماية: استدعاء واحد باسم
    # أي حساب (حتى المشرف الأعلى a0mr1 أو الأدمن نفسه) كان يحوّل دوره إلى
    # 'student' ويسلب صلاحياته. الآن تُرفض أهداف الأدمن والإزالة الذاتية.
    admin = get_current_user()
    username = ""
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = _safe_str(data.get('username'))
        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "لا يوجد حساب بهذا الاسم"}), 404
        if student.role == 'admin':
            return jsonify({"status": "error", "message": "لا يمكن إزالة صلاحية المشرف الأعلى"}), 400
        if student.id == admin.id:
            return jsonify({"status": "error", "message": "لا يمكنك إزالة صلاحياتك بنفسك"}), 400
        if student.role not in SUPERVISOR_ROLES:
            return jsonify({"status": "error", "message": "هذا الحساب ليس مشرفاً"}), 400
        student.role = 'student'
        student.supervisor_college = None
        student.supervisor_specialization = None
        student.supervisor_subject_id = None
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إزالة صلاحية الإشراف"})
    except Exception as e:
        app.logger.exception("فشل إزالة مشرف: username=%s", username)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# نظام بنك الأسئلة والاختبارات وفصل XP
# ==========================================

@app.route('/api/quiz', methods=['GET'])
def get_quiz():
    chapter_name = request.args.get('chapter')
    subject_name = request.args.get('subject')
    user = get_current_user()

    try:
        # ترتيب ثابت حسب id: بدون ORDER BY كان Postgres يعيد الصفوف بترتيب غير
        # مضمون يتغير بين الطلبات، فتظهر أسئلة الاختبار بترتيب مختلف في كل محاولة.
        college_name = request.args.get('college')
        query = QuizQuestion.query.order_by(QuizQuestion.id)
        if chapter_name:
            query = query.filter_by(chapter_name=chapter_name)
        if college_name:
            query = query.filter_by(college=college_name)
        # لا نفلتر بالسنة/الترم: نصوصهما قد تختلف بين صفوف الدروس والأسئلة
        # (مثلاً خطأ إملائي في "السنة الاولئ") فيعيد الفلتر صفر سؤال رغم وجود أسئلة.
        if subject_name:
            query = query.filter_by(subject_name=subject_name)

        if not user or user.role == 'student':
            query = query.filter_by(status='approved')

        # فصل اختبار VIP مغلق: لا تُعاد أسئلته لمن ليس لديه منح تغطيه.
        if chapter_name and (subject_name or college_name):
            _sid = None
            if subject_name:
                _sub = Subject.query.filter_by(name=subject_name)
                if college_name:
                    _sub = _sub.filter_by(college=college_name)
                _sub_row = _sub.first()
                _sid = _sub_row.id if _sub_row else None
            if _quiz_chapter_locked(user, subject_id=_sid, subject_name=subject_name, chapter_name=chapter_name, college=college_name):
                return jsonify({"status": "error", "message": "هذا المحتوى مدفوع، للفتح تواصل معنا", "vip_locked": True, "vip_title": f"أسئلة {subject_name or chapter_name}"}), 403

        questions = query.all()
        return jsonify([q.to_dict() for q in questions])
    except Exception as e:
        db.session.rollback()
        return jsonify([])

@app.route('/api/quiz/attempt', methods=['POST'])
@student_required
def record_quiz_attempt():
    user = get_current_user()
    data = request.get_json(silent=True) or {}

    try:
        score = int(data.get('score', 0))
        total = int(data.get('total_questions', 1))
        perc = float(data.get('percentage', 0))
        subject = str(data.get('subject_name', 'مادة عامة')).strip()
        chapter = str(data.get('chapter_name', 'عام')).strip()

        # فصل اختبار VIP مغلق: لا تُسجَّل محاولة لمن ليس لديه منح تغطيه.
        if _quiz_chapter_locked(user, subject_name=subject, chapter_name=chapter):
            return jsonify({"status": "error", "message": "هذا المحتوى مدفوع، للفتح تواصل معنا", "vip_locked": True}), 403

        attempt = QuizAttempt(
            student_id=user.id,
            subject_name=subject,
            chapter_name=chapter,
            score=score,
            total_questions=total,
            percentage=perc
        )
        db.session.add(attempt)
        db.session.commit()

        # احتساب XP مستقل (يمنح مرة واحدة فقط عند اجتياز الفصل بنجاح)
        profile = UserProfile.query.filter_by(student_id=user.id).first()
        if not profile:
            profile = UserProfile(student_id=user.id)
            db.session.add(profile)
            db.session.commit()
        xp_granted = False
        if perc >= 50 and profile:
            reason = f"اجتياز اختبار: {chapter}"
            already_rewarded = PointTransaction.query.filter_by(student_id=user.id, reason=reason).first()
            if not already_rewarded:
                profile.add_points(20, reason)
                attempt.xp_awarded = 20
                db.session.commit()
                xp_granted = True

        return jsonify({"status": "success", "attempt_id": attempt.id, "xp_awarded": xp_granted})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/import-questions', methods=['POST'])
@can_manage_content
def import_questions():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "يرجى تحديد ملف CSV"}), 400

    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"status": "error", "message": "يجب أن يكون الملف بصيغة CSV"}), 400

    user = get_current_user()
    status = 'approved' if user.role == 'admin' else 'pending_review'

    # مادة مختارة للاستيراد (Flow B): كل الصفوف تُفرض على هذه المادة،
    # وتُتجاهل أعمدة subject/college/year/term الخاصة بالملف (Q8).
    # النطاق يُفحص مرة واحدة هنا بدل فحص كل صف.
    subject = None
    _import_subject_id = request.form.get('subject_id')
    if _import_subject_id not in (None, '', 0, '0'):
        try:
            subject = Subject.query.get(int(_import_subject_id))
        except (TypeError, ValueError):
            subject = None
        if not subject:
            return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
        if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
            return jsonify({"status": "error", "message": "غير مصرح لك بالاستيراد إلى مادة خارج نطاقك"}), 403

    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)
        count = 0
        skipped = 0
        skipped_invalid = 0
        for row in reader:
            qt = (row.get('question_text') or '').strip()
            oa = (row.get('option_a') or '').strip()
            ob = (row.get('option_b') or '').strip()
            oc = (row.get('option_c') or '').strip()
            od = (row.get('option_d') or '').strip()
            oe = (row.get('option_e') or '').strip()
            ca = (row.get('correct_answer') or '').strip()
            # ملفات CSV القديمة كانت تضع كلمة "اختيار" في question_text والسؤال
            # الحقيقي في option_a؛ نعيد ترتيب الحقول عند الاستيراد حتى لا تتكرر
            # المشكلة: نص السؤال في question_text والإجابات في option_a..option_e.
            if (_is_placeholder_question_title(qt) or qt == "") and oa:
                # حرف/رقم الإجابة الصحيحة كان يشير إلى أعمدة الملف الأصلية
                # (option_a = نص السؤال)، فنحوّله إلى نص الإجابة قبل الإزاحة.
                # المؤشر 0 يشير إلى نص السؤال نفسه فلا يُحلّ (لا إجابة هناك).
                orig_opts = [o for o in (oa, ob, oc, od, oe) if o]
                if len(ca) == 1 and ca.upper() in "ABCDE":
                    _idx = "ABCDE".index(ca.upper())
                    if 0 < _idx < len(orig_opts):
                        ca = orig_opts[_idx]
                elif len(ca) == 1 and ca in "12345":
                    _idx = int(ca) - 1
                    if 0 < _idx < len(orig_opts):
                        ca = orig_opts[_idx]
                qt, oa, ob, oc, od, oe = oa, ob, oc, od, oe, ""
            if subject is not None:
                _row_college = subject.college
                _row_subject = subject.name
                _row_year = subject.year
                _row_term = subject.term
                _row_subject_id = subject.id
            else:
                _row_college = (row.get('college') or '').strip() or 'كلية الهندسة'
                _row_subject = (row.get('subject_name') or 'عام').strip()
                # سنة/ترم الملف تُطبَّعان إلى القيم القياسية؛ صف بسنة أو ترم
                # غير صالحين يُتخطى بدل إنشاء سؤال مرتبط بمادة مخفية.
                _row_year = _normalize_year((row.get('year') or '').strip() or 'السنة الأولى')
                _row_term = _normalize_term((row.get('term') or '').strip() or 'الترم الأول')
                if not _row_year or not _row_term:
                    skipped_invalid += 1
                    continue
                if user.role in SUPERVISOR_ROLES and not _in_scope(user, _row_college, _row_subject):
                    skipped += 1
                    continue
                # الاستيراد بلا مادة مختارة ينشئ سجل المادة تلقائياً (get-or-create)
                # حتى تظهر في قوائم الإدارة («المواد»، محدد الاستيراد، مرشح
                # الأسئلة) وتقبل إعادة التسمية والحذف؛ بلا ذلك تبقى الأسئلة
                # بلا subject_id وتظهر المادة في الصفحة الرئيسية فقط عبر
                # تجميع النص الاحتياطي. النطاق فُحص أعلاه قبل الوصول هنا،
                # فمشرف التخصص لا ينشئ إلا مادته، ومشرف الكلية إلا في كليته.
                _matched = Subject.query.filter_by(college=_row_college, year=_row_year, term=_row_term, name=_row_subject).first()
                if _matched is None:
                    if not College.query.filter_by(name=_row_college).first():
                        db.session.add(College(name=_row_college))
                    _matched = Subject(name=_row_subject, college=_row_college, year=_row_year, term=_row_term, created_by=user.id)
                    db.session.add(_matched)
                    db.session.flush()  # يمنح الصف id قبل ربط الأسئلة به
                _row_subject_id = _matched.id
            q = QuizQuestion(
                year=_row_year,
                term=_row_term,
                college=_row_college,
                subject_name=_row_subject,
                subject_id=_row_subject_id,
                chapter_name=row.get('chapter_name', 'عام'),
                question_text=qt,
                option_a=oa,
                option_b=ob,
                option_c=oc,
                option_d=od,
                option_e=oe,
                correct_answer=ca,
                explanation=row.get('explanation', '').strip(),
                status=status,
                created_by=user.id
            )
            if q.question_text and q.correct_answer:
                db.session.add(q)
                count += 1
        db.session.commit()
        _msg = f"تم استيراد {count} سؤال بنجاح كـ ({status})"
        if skipped:
            _msg += f" — تم تخطي {skipped} سؤالاً خارج نطاقك"
        if skipped_invalid:
            _msg += f" — تم تخطي {skipped_invalid} سؤالاً بسبب سنة أو ترم غير صالحين (المقبول: {_YEAR_VALUES_TEXT} / {_TERM_VALUES_TEXT})"
        return jsonify({"status": "success", "message": _msg})
    except Exception as e:
        app.logger.exception("فشل استيراد الأسئلة")
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ في قراءة الملف: {str(e)}"}), 500

# ==========================================
# استفسارات الطلاب (Lesson Questions)
# ==========================================

@app.route('/ask-question', methods=['POST'])
def submit_question():
    try:
        data = request.get_json(silent=True) or request.form or {}
        user = get_current_user()

        subject_id = data.get('subject_id')
        chapter_name = data.get('chapter_name')
        student_name = (data.get('student_name') or (user.name if user else 'طالب')).strip()
        question_text = (data.get('question_text') or '').strip()

        if not question_text or not subject_id or not chapter_name:
            return jsonify({"status": "error", "message": "بيانات غير مكتملة"}), 400

        q = LessonQuestion(
            subject_id=subject_id,
            chapter_name=chapter_name,
            student_name=student_name,
            student_id=user.id if user else None,
            question_text=question_text
        )
        db.session.add(q)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إرسال السؤال بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/questions', methods=['GET'])
@can_manage_content
def get_pending_questions():
    try:
        user = get_current_user()
        qs = LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()
        if user and user.role in SUPERVISOR_ROLES:
            qs = [q for q in qs if _question_in_scope(q, user)]
        return jsonify([q.to_dict() for q in qs])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@can_manage_content
def reply_question(question_id):
    try:
        user = get_current_user()
        q = LessonQuestion.query.get_or_404(question_id)
        if user.role in SUPERVISOR_ROLES and not _question_in_scope(q, user):
            return jsonify({"status": "error", "message": "غير مصرح لك بالرد على سؤال خارج نطاقك"}), 403
        data = request.get_json(silent=True) or request.form or {}
        ans = (data.get('answer_text') or '').strip()

        if not ans:
            return jsonify({"status": "error", "message": "الرد مطلوب"}), 400

        q.answer_text = ans
        q.is_answered = True
        q.answered_by = user.id
        q.answered_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الرد بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_question(question_id):
    try:
        q = LessonQuestion.query.get_or_404(question_id)
        db.session.delete(q)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# المساعد الذكي وتقديرات الدرجات
# ==========================================

def calculate_grade_estimation(score):
    """تقدير الدرجة: يقبل مقياس 0-100 أو 0-4، ويعيد التقدير كنص عربي مع الحرف."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise ValueError("الدرجة يجب أن تكون رقماً")
    if value <= 4:
        # مقياس 0-4 (معدل)
        if value >= 3.5:
            return "ممتاز (A)"
        if value >= 3.0:
            return "جيد جداً (B)"
        if value >= 2.0:
            return "جيد (C)"
        if value >= 1.0:
            return "مقبول (D)"
        return "راسب (F)"
    if value >= 90:
        return "ممتاز (A)"
    if value >= 80:
        return "جيد جداً (B)"
    if value >= 70:
        return "جيد (C)"
    if value >= 60:
        return "مقبول (D)"
    return "راسب (F)"

@app.route('/calculate-grade', methods=['POST'])
def calculate_grade():
    try:
        data = request.get_json(silent=True) or request.form or {}
        score = data.get('score')
        if score is None:
            return jsonify({"status": "error", "message": "يرجى إدخال الدرجة"}), 400

        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الدرجة يجب أن تكون رقماً"}), 400
        if numeric_score < 0 or numeric_score > 100:
            return jsonify({"status": "error", "message": "الدرجة يجب أن تكون بين 0 و 100"}), 400

        grade = calculate_grade_estimation(numeric_score)
        return jsonify({"status": "success", "score": numeric_score, "grade": grade})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        user_message = data.get('message') or request.form.get('message') or ''
        user_message = str(user_message).strip()

        if not user_message:
            file = request.files.get('file')
            user_message = "اشرح هذا المحتوى بالتفصيل خطوة بخطوة" if file else "مرحباً بك"

        messages = [
            {
                "role": "system",
                "content": "أنت المساعد الأكاديمي الذكي لمنصة عمرو أكاديمي. قدّم إجابات علمية وهندسية دقيقة وبسيطة وباللغة العربية خطوة بخطوة."
            },
            {"role": "user", "content": user_message}
        ]

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.5
        )

        reply = response.choices[0].message.content
        return jsonify({"reply": reply})
    except Exception:
        app.logger.exception("ask_ai: failed")
        return jsonify({"reply": AI_UNAVAILABLE_MSG}), 500

# ==========================================
# نظام التخطيط الدراسي والذكاء الاصطناعي الكامل
# ==========================================

@app.route('/simulators')
def simulators():
    user = get_current_user()
    if _service_locked(user, 'simulator'):
        return render_template('vip_locked.html', service_name='المختبر', service_key='simulator', student_id=session.get('student_id'))
    return render_template('simulators.html', student_id=session.get('student_id'))

@app.route('/planner')
def planner():
    user = get_current_user()
    if _service_locked(user, 'planner'):
        return render_template('vip_locked.html', service_name='الخطة الدراسية', service_key='planner', student_id=user.id if user else None)
    return render_template('planner.html', student_id=user.id if user else None, student_name=user.name if user else None)

@app.route('/guide')
def guide():
    """صفحة دليل الطالب (آراء الطلاب عن الدكاترة): قراءة الآراء المعتمدة
    مفتوحة للجميع، وإضافة رأي أو التصويت تتطلب تسجيل دخول."""
    user = get_current_user()
    if _service_locked(user, 'guide'):
        return render_template('vip_locked.html', service_name='دليل الطالب', service_key='guide', student_id=user.id if user else None)
    try:
        colleges = [c.to_dict() for c in College.query.order_by(College.id).all()]
    except Exception:
        db.session.rollback()
        colleges = []
    if not colleges:
        colleges = [{"id": 0, "name": "كلية الهندسة"}]
    return render_template('guide.html', student_id=user.id if user else None,
                           student_name=user.name if user else None, colleges=colleges)

# ==========================================
# نظام «تعلم اللغة مع Amr» — مسارات API
# ==========================================
# كل مسارات API تتطلب تسجيل دخول. GET /language يعرض الصفحة حتى
# للزائر (مع logged_out=True) فيعرض القالب دعوة لتسجيل الدخول.

# ==========================================
# وكيل صوتي للنطق — GET /audio/en/<word>.mp3
# ==========================================
# لماذا وكيل من الخادم؟ ملفات MP3 ثنائية ولا يمكن دفعها إلى GitHub
# عبر أدوات الدفع المتاحة (نص UTF-8 فقط)، وهاتف عمرو في اليمن لا يصل
# إلى Google TTS ولا يملك صوت TTS إنجليزياً. الخادم (Render) يجلب
# الصوت من Google ويقدّمه من نفس المصدر للهاتف.
# ليس وكيلاً مفتوحاً: الكلمة يجب أن تكون موجودة في جدول LanguageWord
# وأن تطابق ^[A-Za-z0-9 \-']+$ بعد فك ترميز URL. أي فشل في الجلب
# يعيد 502 فينتقل المتصفح للطريقة التالية في سلسلة النطق.
# لا يتطلب تسجيل دخول (مستثنى في before_request و after_request مثل
# /static). التخزين المؤقت على قرص مؤقت (/tmp/lang_audio) — يعيد
# الجلب بعد كل إعادة نشر، وهذا مقبول.

_AUDIO_CACHE_DIR = os.environ.get('LANG_AUDIO_CACHE_DIR') or '/tmp/lang_audio'
_AUDIO_TTS_URL = ('https://translate.google.com/translate_tts'
                  '?ie=UTF-8&tl=en&client=tw-ob&q={q}')
_AUDIO_TTS_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                 '(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36')
_AUDIO_WORD_RE = re.compile(r"^[A-Za-z0-9 \-']+$")


def _audio_is_mp3(data):
    """فحص سحري بسيط: رأس ID3 أو مزامنة إطار MPEG (0xFF ثم 0xE0+)."""
    if not data or len(data) <= 1024:
        return False
    if data[:3] == b'ID3':
        return True
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


@app.route('/audio/en/<path:filename>')
def language_audio(filename):
    """يخدم نطق كلمة من كلمات اللغة عبر وكيل Google TTS مع تخزين مؤقت.

    /audio/en/house.mp3 — 200 + audio/mpeg عند النجاح،
    404 لكلمة غير معروفة أو غير صالحة، 502 عند فشل جلب Google."""
    if not filename.endswith('.mp3'):
        return jsonify({"status": "error", "message": "not found"}), 404
    word = _url_unquote(filename[:-4])
    if not _AUDIO_WORD_RE.match(word):
        return jsonify({"status": "error", "message": "not found"}), 404
    try:
        known = LanguageWord.query.filter_by(word=word).first() is not None
    except Exception:
        db.session.rollback()
        return jsonify({"status": "error", "message": "audio unavailable"}), 502
    if not known:
        return jsonify({"status": "error", "message": "not found"}), 404

    try:
        os.makedirs(_AUDIO_CACHE_DIR, exist_ok=True)
    except Exception:
        return jsonify({"status": "error", "message": "audio unavailable"}), 502
    cache_name = _url_quote(word, safe="-_.!~*'()") + '.mp3'
    cache_path = os.path.join(_AUDIO_CACHE_DIR, cache_name)

    try:
        if os.path.isfile(cache_path):
            with open(cache_path, 'rb') as fh:
                cached = fh.read()
            if _audio_is_mp3(cached):
                return send_file(cache_path, mimetype='audio/mpeg', max_age=86400)
    except Exception:
        pass

    try:
        resp = requests.get(
            _AUDIO_TTS_URL.format(q=_url_quote(word, safe='')),
            headers={'User-Agent': _AUDIO_TTS_UA}, timeout=10)
    except Exception:
        return jsonify({"status": "error", "message": "audio unavailable"}), 502
    ctype = (resp.headers.get('Content-Type') or '')
    if (resp.status_code != 200 or 'audio/mpeg' not in ctype
            or not _audio_is_mp3(resp.content)):
        return jsonify({"status": "error", "message": "audio unavailable"}), 502

    tmp_path = cache_path + '.tmp.%d' % os.getpid()
    try:
        with open(tmp_path, 'wb') as fh:
            fh.write(resp.content)
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return send_file(io.BytesIO(resp.content), mimetype='audio/mpeg',
                         max_age=86400)
    return send_file(cache_path, mimetype='audio/mpeg', max_age=86400)


@app.route('/language')
def language_page():
    user = get_current_user()
    if not user:
        return render_template('language.html', student_id=None, student_name=None,
                               logged_out=True, default_track='general')
    # لا ننشئ ملف اللغة هنا: الطالب الجديد بلا خطط يجب أن يرى بطاقة
    # إنشاء الخطة أولاً (لا خطة تُرحَّل له قبل أن يختار).
    plans = LanguagePlan.query.filter_by(student_id=user.id).order_by(LanguagePlan.id).all()
    if plans:
        current = next((p for p in plans if p.is_current), plans[0])
        default_track = current.track
    else:
        prof = UserLanguageProfile.query.filter_by(student_id=user.id).first()
        default_track = prof.track if prof else _default_language_track(user)
    return render_template('language.html', student_id=user.id, student_name=user.name,
                           logged_out=False, default_track=default_track)

@app.route('/api/language/profile', methods=['GET'])
@student_required
def language_profile_get():
    user = get_current_user()
    lang_profile = _get_or_create_language_profile(user)
    return jsonify({"status": "success", **_language_profile_payload(user, lang_profile)})

@app.route('/api/language/profile', methods=['POST'])
@student_required
def language_profile_update():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    lang_profile = _get_or_create_language_profile(user)
    if 'track' in data:
        track = str(data.get('track') or '').strip()
        if track not in LANGUAGE_TRACKS:
            return jsonify({"status": "error", "message": "المسار غير صالح (medical / engineering / general)"}), 400
        # تغيير المسار = تبديل الخطة: إن وُجدت خطة بهذا المسار فعّلها،
        # وإلا غيّر مسار الخطة الحالية إليه (لا خطة أخرى تحمل هذا المسار).
        existing = LanguagePlan.query.filter_by(student_id=user.id, track=track).first()
        if existing:
            LanguagePlan.query.filter_by(student_id=user.id).update({"is_current": False})
            existing.is_current = True
            lang_profile.track = track
        else:
            plan = _ensure_language_plan(user)
            plan.track = track
            lang_profile.track = track
    if 'daily_target' in data:
        try:
            target = int(data.get('daily_target'))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الهدف اليومي غير صالح (5 أو 10 أو 20)"}), 400
        if target not in LANGUAGE_DAILY_TARGETS:
            return jsonify({"status": "error", "message": "الهدف اليومي غير صالح (5 أو 10 أو 20)"}), 400
        plan = _ensure_language_plan(user)
        plan.daily_target = target
        lang_profile.daily_target = target
    lang_profile.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"status": "success", **_language_profile_payload(user, lang_profile)})

@app.route('/api/language/plans', methods=['GET'])
@student_required
def language_plans_list():
    """كل خطط الطالب مع تقدم كل خطة. ترحيل لمرة واحدة: ملف لغة قديم
    بلا خطط يتحول إلى خطة واحدة حالية من مساره وهدفه. الطالب الجديد
    بلا ملف أصلاً يرجع قائمة فارغة (فيرى بطاقة إنشاء الخطة)."""
    user = get_current_user()
    plans = LanguagePlan.query.filter_by(student_id=user.id).order_by(LanguagePlan.id).all()
    if not plans:
        profile = UserLanguageProfile.query.filter_by(student_id=user.id).first()
        if profile is not None:
            plan = LanguagePlan(student_id=user.id, track=profile.track,
                                daily_target=profile.daily_target, is_current=True)
            db.session.add(plan)
            db.session.commit()
            plans = [plan]
    out = _plans_with_progress(user)
    current_id = next((p["id"] for p in out if p["is_current"]), out[0]["id"] if out else None)
    return jsonify({"status": "success", "plans": out, "current_plan_id": current_id})

@app.route('/api/language/plans', methods=['POST'])
@student_required
def language_plan_create():
    """إنشاء خطة تعلم (مسار + هدف يومي). مسار واحد لكل خطة؛
    أول خطة للطالب تصبح الحالية تلقائياً."""
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    track = str(data.get('track') or '').strip()
    if track not in LANGUAGE_TRACKS:
        return jsonify({"status": "error", "message": "المسار غير صالح (medical / engineering / general)"}), 400
    try:
        target = int(data.get('daily_target'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "الهدف اليومي غير صالح (5 أو 10 أو 20)"}), 400
    if target not in LANGUAGE_DAILY_TARGETS:
        return jsonify({"status": "error", "message": "الهدف اليومي غير صالح (5 أو 10 أو 20)"}), 400
    if LanguagePlan.query.filter_by(student_id=user.id, track=track).first():
        return jsonify({"status": "error", "message": "لديك خطة بهذا المسار بالفعل — فعّلها بدل إنشاء واحدة جديدة"}), 400
    first = LanguagePlan.query.filter_by(student_id=user.id).count() == 0
    plan = LanguagePlan(student_id=user.id, track=track, daily_target=target, is_current=first)
    db.session.add(plan)
    # مرآة الملف القديم مع الخطة الحالية (أو إنشاؤه للطالب الجديد).
    profile = _get_or_create_language_profile(user)
    if first:
        profile.track = track
        profile.daily_target = target
        profile.updated_at = datetime.utcnow()
    db.session.commit()
    totals = _word_counts_by_track()
    return jsonify({"status": "success",
                    "plan": plan.to_dict(mastered=_mastered_count(user.id, track),
                                         total_in_track=totals.get(track, 0))}), 201

@app.route('/api/language/plans/<int:plan_id>/activate', methods=['POST'])
@student_required
def language_plan_activate(plan_id):
    """تفعيل خطة (جعلها الحالية)؛ الدرس والملف يتبعانها فوراً."""
    user = get_current_user()
    plan = LanguagePlan.query.filter_by(id=plan_id, student_id=user.id).first()
    if not plan:
        return jsonify({"status": "error", "message": "الخطة غير موجودة"}), 404
    LanguagePlan.query.filter_by(student_id=user.id).update({"is_current": False})
    plan.is_current = True
    db.session.commit()
    _sync_profile_to_plan(user, plan)
    totals = _word_counts_by_track()
    return jsonify({"status": "success",
                    "plan": plan.to_dict(mastered=_mastered_count(user.id, plan.track),
                                         total_in_track=totals.get(plan.track, 0)),
                    "current_plan_id": plan.id})

@app.route('/api/language/plans/<int:plan_id>', methods=['DELETE'])
@student_required
def language_plan_delete(plan_id):
    """حذف خطة. الكلمات المتقَنة والـ XP لا تُمس (مربوطة بالطالب
    والكلمة، لا بالخطة). إن حُذفت الخطة الحالية وبقيت خطط أخرى،
    تُفعَّل أقدمها."""
    user = get_current_user()
    plan = LanguagePlan.query.filter_by(id=plan_id, student_id=user.id).first()
    if not plan:
        return jsonify({"status": "error", "message": "الخطة غير موجودة"}), 404
    was_current = bool(plan.is_current)
    db.session.delete(plan)
    db.session.commit()
    remaining = LanguagePlan.query.filter_by(student_id=user.id).order_by(LanguagePlan.id).all()
    new_current = None
    if remaining:
        if was_current:
            new_current = remaining[0]
            new_current.is_current = True
            db.session.commit()
            _sync_profile_to_plan(user, new_current)
        else:
            new_current = next((p for p in remaining if p.is_current), remaining[0])
    out = _plans_with_progress(user)
    return jsonify({"status": "success", "plans": out,
                    "current_plan_id": new_current.id if new_current else None})

@app.route('/api/language/today', methods=['GET'])
@student_required
def language_today():
    user = get_current_user()
    plan = _ensure_language_plan(user)
    lang_profile = _get_or_create_language_profile(user)
    track = plan.track
    # المراجعة أولاً: كلمات بحالة seen (غير متقَنة) في هذا المسار،
    # الأقدم مشاهدة أولاً، بحد أقصى 20.
    review_rows = (db.session.query(UserLanguageWord, LanguageWord)
                   .join(LanguageWord, UserLanguageWord.word_id == LanguageWord.id)
                   .filter(UserLanguageWord.student_id == user.id,
                           UserLanguageWord.state == 'seen',
                           LanguageWord.track == track)
                   .order_by(UserLanguageWord.last_seen.asc().nullsfirst())
                   .limit(20).all())
    review = [w.to_dict(state='seen') for _rel, w in review_rows]
    # الجديد: كلمات في المسار لم يرها الطالب أبداً، بترتيب id، حتى الهدف اليومي.
    seen_ids = (db.session.query(UserLanguageWord.word_id)
                .filter_by(student_id=user.id))
    new_words = (LanguageWord.query
                 .filter(LanguageWord.track == track,
                         ~LanguageWord.id.in_(seen_ids))
                 .order_by(LanguageWord.id)
                 .limit(plan.daily_target)
                 .all())
    new = [w.to_dict(state='new') for w in new_words]
    payload = _language_profile_payload(user, lang_profile)
    return jsonify({"status": "success", "review": review, "new": new,
                    "mastered": payload["mastered"], "level": payload["level"],
                    "streak": payload["streak"], "daily_target": payload["daily_target"],
                    "track": payload["track"]})

@app.route('/api/language/answer', methods=['POST'])
@student_required
def language_answer():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    try:
        word_id = int(data.get('word_id'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "word_id غير صالح"}), 400
    correct = bool(data.get('correct'))
    # quiz_type يُقبل ويُتجاهل حالياً (محفوظ لتوافق الواجهة).
    word = LanguageWord.query.get(word_id)
    if not word:
        return jsonify({"status": "error", "message": "الكلمة غير موجودة"}), 404
    plan = _ensure_language_plan(user)
    lang_profile = _get_or_create_language_profile(user)
    profile = _get_or_create_user_profile(user.id)
    now = datetime.utcnow()
    rel = UserLanguageWord.query.filter_by(student_id=user.id, word_id=word_id).first()
    if not rel:
        rel = UserLanguageWord(student_id=user.id, word_id=word_id, state='seen')
        db.session.add(rel)
    rel.seen_count = (rel.seen_count or 0) + 1
    rel.last_seen = now
    xp_awarded = 0
    if correct:
        rel.correct_count = (rel.correct_count or 0) + 1
        if rel.state != 'mastered':
            rel.state = 'mastered'
            rel.mastered_at = now
            # +2 XP مرة واحدة فقط: السبب فريد لكل كلمة، وadd_points
            # يتخطى المنح إن وُجدت معاملة بالسبب نفسه من قبل.
            points_before = profile.points or 0
            profile.add_points(LANGUAGE_WORD_XP, reason=f"تعلم كلمة إنجليزية #{word_id}")
            db.session.refresh(profile)
            xp_awarded = max(0, (profile.points or 0) - points_before)
        else:
            db.session.commit()
    else:
        # خطأ: لا XP، والحالة تبقى كما هي (seen تبقى seen، والمتقَنة لا تُسحب).
        db.session.commit()
    mastered_count = _mastered_count(user.id, plan.track)
    return jsonify({"status": "success", "mastered": rel.state == 'mastered',
                    "xp_awarded": xp_awarded, "total_points": profile.total_points or 0,
                    "xp_from_language": _xp_from_language(user.id),
                    "level": _language_level(mastered_count), "mastered_count": mastered_count})

@app.route('/api/language/complete', methods=['POST'])
@student_required
def language_complete():
    user = get_current_user()
    plan = _ensure_language_plan(user)
    lang_profile = _get_or_create_language_profile(user)
    profile = _get_or_create_user_profile(user.id)
    today = datetime.utcnow().date()
    today_iso = today.isoformat()
    reason = f"إكمال درس اللغة {today_iso}"
    if lang_profile.last_lesson_date == today or \
            PointTransaction.query.filter_by(student_id=user.id, reason=reason).first():
        return jsonify({"status": "success", "already_completed": True, "xp_awarded": 0,
                        "streak": lang_profile.streak_days or 0,
                        "level": _language_level(_mastered_count(user.id, plan.track)),
                        "total_points": profile.total_points or 0,
                        "xp_from_language": _xp_from_language(user.id)})
    yesterday = today - timedelta(days=1)
    if lang_profile.last_lesson_date == yesterday:
        lang_profile.streak_days = (lang_profile.streak_days or 0) + 1
    else:
        lang_profile.streak_days = 1
    lang_profile.last_lesson_date = today
    lang_profile.updated_at = datetime.utcnow()
    # +10 XP مرة واحدة يومياً: التاريخ في السبب يجعل add_points
    # يتخطى أي منح ثانٍ في اليوم نفسه.
    points_before = profile.points or 0
    profile.add_points(LANGUAGE_LESSON_XP, reason=reason)
    db.session.refresh(profile)
    xp_awarded = max(0, (profile.points or 0) - points_before)
    mastered_count = _mastered_count(user.id, plan.track)
    return jsonify({"status": "success", "already_completed": False, "xp_awarded": xp_awarded,
                    "streak": lang_profile.streak_days or 0,
                    "level": _language_level(mastered_count),
                    "total_points": profile.total_points or 0,
                    "xp_from_language": _xp_from_language(user.id)})

@app.route('/api/language/progress', methods=['GET'])
@student_required
def language_progress():
    user = get_current_user()
    plan = _ensure_language_plan(user)
    lang_profile = _get_or_create_language_profile(user)
    mastered = _mastered_count(user.id, plan.track)
    return jsonify({"status": "success", "mastered": mastered,
                    "level": _language_level(mastered),
                    "progress_pct": mastered % 100,
                    "streak": lang_profile.streak_days or 0,
                    "daily_target": plan.daily_target,
                    "track": plan.track,
                    "current_plan_id": plan.id,
                    "counts": _word_counts_by_track(),
                    "mastered_by_track": {t: _mastered_count(user.id, t) for t in LANGUAGE_TRACKS}})

@app.route('/admin/import-language-words', methods=['POST'])
@admin_required
def admin_import_language_words():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "يرجى تحديد ملف CSV"}), 400
    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"status": "error", "message": "يجب أن يكون الملف بصيغة CSV"}), 400
    try:
        result = import_language_words_csv(file)
    except Exception as e:
        app.logger.exception("فشل استيراد كلمات اللغة")
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ في قراءة الملف: {str(e)}"}), 500
    msg = f"تم استيراد {result['imported']} كلمة بنجاح"
    if result['skipped']:
        msg += f" — تم تخطي {result['skipped']} كلمة مكررة"
    if result['skipped_invalid']:
        msg += f" — تم تخطي {result['skipped_invalid']} صفاً غير صالح"
    return jsonify({"status": "success", "message": msg,
                    "imported": result['imported'], "skipped": result['skipped'],
                    "skipped_invalid": result['skipped_invalid']})

@app.route('/admin/language-stats', methods=['GET'])
@admin_required
def admin_language_stats():
    counts = _word_counts_by_track()
    return jsonify({"status": "success", "counts": counts,
                    "total": sum(counts.values())})

# ==========================================
# نظام «خدمات» — متاجر داخل الأكاديمية
# ==========================================
# متجر: اسم + وصف + رقم واتساب + مدير + حالة (active/paused).
# منتج: اسم + سعر بالريال + رابط صورة + تفاصيل اختيارية.
# التسوق مفتوح دون تسجيل دخول؛ الطلب يفتح wa.me برسالة جاهزة.
# الجدولان الجديدان (stores/products) ينشئهما db.create_all() — لا حاجة لـ ALTER TABLE.

@app.route('/services')
def services():
    """صفحة «خدمات»: قائمة المتاجر الظاهرة + منتجات المتجر المختار.
    التسوق مفتوح للجميع دون تسجيل دخول."""
    user = get_current_user()
    return render_template('services.html', student_id=user.id if user else None,
                           student_name=user.name if user else None)

@app.route('/api/stores', methods=['GET'])
def get_public_stores():
    """المتاجر الظاهرة للعموم مع عدد المنتجات."""
    try:
        stores = Store.query.filter_by(status='active').order_by(Store.id).all()
        return jsonify([s.to_dict() for s in stores])
    except Exception:
        app.logger.exception("فشل جلب المتاجر العامة")
        db.session.rollback()
        return jsonify([])

@app.route('/api/stores/<int:store_id>/products', methods=['GET'])
def get_store_products(store_id):
    """منتجات متجر ظاهر (للعموم)."""
    try:
        store = Store.query.get(store_id)
        if not store or store.status != 'active':
            return jsonify({"status": "error", "message": "المتجر غير موجود أو موقوف"}), 404
        products = Product.query.filter_by(store_id=store_id).order_by(Product.id).all()
        return jsonify([p.to_dict() for p in products])
    except Exception:
        app.logger.exception("فشل جلب منتجات المتجر: store_id=%s", store_id)
        db.session.rollback()
        return jsonify([])

@app.route('/api/stores/<int:store_id>/products', methods=['POST'])
@store_manager_required
def add_store_product(store_id):
    """مدير المتجر (أو الأدمن) يضيف منتجاً لمتجره."""
    try:
        user = get_current_user()
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        if not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المتجر ليس من متاجرك"}), 403
        data = request.get_json(silent=True) or {}
        name = _safe_str(data.get('name'))
        image_url = _safe_str(data.get('image_url'))
        details = _safe_str(data.get('details'))
        currency = str(data.get('currency') or 'YER').strip().upper()
        if currency not in ('YER', 'SAR', 'USD'):
            return jsonify({"status": "error", "message": "العملة غير صالحة — اختر ريال يمني أو ريال سعودي أو دولار"}), 400
        try:
            price = int(data.get('price'))
        except (TypeError, ValueError):
            price = 0
        if not name:
            return jsonify({"status": "error", "message": "اسم المنتج مطلوب"}), 400
        if not price or price <= 0:
            return jsonify({"status": "error", "message": "السعر يجب أن يكون رقماً صحيحاً أكبر من صفر"}), 400
        product = Product(store_id=store_id, name=name, price=price, currency=currency,
                          image_url=image_url or None, details=details or None)
        db.session.add(product)
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إضافة المنتج بنجاح", "product": product.to_dict()})
    except Exception:
        app.logger.exception("فشل إضافة منتج: store_id=%s", store_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إضافة المنتج"}), 500

@app.route('/api/products/<int:product_id>/delete', methods=['POST'])
@store_manager_required
def delete_store_product(product_id):
    """مدير متجره (أو الأدمن) يحذف منتجاً."""
    try:
        user = get_current_user()
        product = Product.query.get(product_id)
        if not product:
            return jsonify({"status": "error", "message": "المنتج غير موجود"}), 404
        store = Store.query.get(product.store_id)
        if not store or not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المنتج ليس في متجرك"}), 403
        db.session.delete(product)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف المنتج بنجاح"})
    except Exception:
        app.logger.exception("فشل حذف منتج: product_id=%s", product_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حذف المنتج"}), 500

@app.route('/api/products/<int:product_id>/update', methods=['POST'])
@store_manager_required
def update_store_product(product_id):
    """مدير متجره (أو الأدمن) يعدّل منتجاً قائماً: الاسم/السعر/العملة/التفاصيل/الصورة.
    حقول جزئية: يُحدَّث فقط ما يُرسَل؛ الاسم (إن أُرسل) غير فارغ، والسعر (إن أُرسل) > 0."""
    try:
        user = get_current_user()
        product = Product.query.get(product_id)
        if not product:
            return jsonify({"status": "error", "message": "المنتج غير موجود"}), 404
        store = Store.query.get(product.store_id)
        if not store or not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المنتج ليس في متجرك"}), 403
        data = request.get_json(silent=True) or {}
        if 'name' in data:
            name = _safe_str(data.get('name'))
            if not name:
                return jsonify({"status": "error", "message": "اسم المنتج مطلوب"}), 400
            product.name = name
        if 'price' in data:
            try:
                price = int(data.get('price'))
            except (TypeError, ValueError):
                price = 0
            if not price or price <= 0:
                return jsonify({"status": "error", "message": "السعر يجب أن يكون رقماً صحيحاً أكبر من صفر"}), 400
            product.price = price
        if 'currency' in data:
            currency = str(data.get('currency') or '').strip().upper()
            if currency not in ('YER', 'SAR', 'USD'):
                return jsonify({"status": "error", "message": "العملة غير صالحة — اختر ريال يمني أو ريال سعودي أو دولار"}), 400
            product.currency = currency
        if 'image_url' in data:
            product.image_url = _safe_str(data.get('image_url')) or None
        if 'details' in data:
            product.details = _safe_str(data.get('details')) or None
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حفظ تعديل المنتج بنجاح", "product": product.to_dict()})
    except Exception:
        app.logger.exception("فشل تحديث منتج: product_id=%s", product_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حفظ تعديل المنتج"}), 500

@app.route('/api/stores/<int:store_id>/update', methods=['POST'])
@store_manager_required
def update_my_store(store_id):
    """مدير المتجر (أو الأدمن) يعدّل اسم متجره وصورته ورقم واتسابه."""
    name = ""
    try:
        user = get_current_user()
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        if not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المتجر ليس من متاجرك"}), 403
        data = request.get_json(silent=True) or {}
        name = _safe_str(data.get('name'))
        image_url = _safe_str(data.get('image_url'))
        whatsapp = normalize_whatsapp(data.get('whatsapp_number'))
        if not name:
            return jsonify({"status": "error", "message": "اسم المتجر مطلوب"}), 400
        if not whatsapp:
            return jsonify({"status": "error", "message": "رقم الواتساب مطلوب"}), 400
        if len(image_url) > 500:
            return jsonify({"status": "error", "message": "رابط صورة المتجر طويل جداً"}), 400
        store.name = name
        store.image_url = image_url or None
        store.whatsapp_number = whatsapp
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حفظ تعديلات المتجر", "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل تحديث متجر المدير: store_id=%s name=%s", store_id, name)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حفظ تعديلات المتجر"}), 500

@app.route('/api/stores/<int:store_id>/whatsapp', methods=['POST'])
@store_manager_required
def update_store_whatsapp(store_id):
    """مدير المتجر (أو الأدمن) يعدّل رقم واتساب متجره — مع التطبيع."""
    try:
        user = get_current_user()
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        if not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المتجر ليس من متاجرك"}), 403
        data = request.get_json(silent=True) or {}
        number = normalize_whatsapp(data.get('whatsapp_number'))
        if not number:
            return jsonify({"status": "error", "message": "رقم الواتساب مطلوب"}), 400
        store.whatsapp_number = number
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث رقم الواتساب", "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل تحديث واتساب المتجر: store_id=%s", store_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تحديث الرقم"}), 500

@app.route('/admin/stores', methods=['GET'])
@admin_required
def get_stores_admin():
    """كل المتاجر مع الحالة والمدير وعدد المنتجات (للمشرف الأعلى)."""
    try:
        stores = Store.query.order_by(Store.id).all()
        return jsonify([s.to_dict() for s in stores])
    except Exception:
        app.logger.exception("فشل جلب المتاجر للإدارة")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-store', methods=['POST'])
@admin_required
def add_store():
    name = ""
    try:
        user = get_current_user()
        data = request.get_json(silent=True) or {}
        name = _safe_str(data.get('name'))
        description = _safe_str(data.get('description'))
        image_url = _safe_str(data.get('image_url'))
        whatsapp = normalize_whatsapp(data.get('whatsapp_number'))
        manager_id = data.get('manager_id')
        if not name:
            return jsonify({"status": "error", "message": "اسم المتجر مطلوب"}), 400
        if not whatsapp:
            return jsonify({"status": "error", "message": "رقم واتساب المتجر مطلوب"}), 400
        if len(image_url) > 500:
            return jsonify({"status": "error", "message": "رابط صورة المتجر طويل جداً"}), 400
        manager = None
        if manager_id:
            try:
                manager = Student.query.get(int(manager_id))
            except (TypeError, ValueError):
                manager = None
            if not manager:
                return jsonify({"status": "error", "message": "المدير المختار غير موجود"}), 400
            if manager.role == 'admin':
                return jsonify({"status": "error", "message": "هذا الحساب مشرف أعلى بالفعل"}), 400
        store = Store(name=name, description=description or None, whatsapp_number=whatsapp,
                      image_url=image_url or None, status='active', manager_id=manager.id if manager else None,
                      created_by=user.id if user else None)
        db.session.add(store)
        if manager:
            manager.role = 'store_manager'
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إنشاء المتجر بنجاح", "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل إنشاء متجر: name=%s", name)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إنشاء المتجر"}), 500

@app.route('/admin/edit-store/<int:store_id>', methods=['POST'])
@admin_required
def edit_store(store_id):
    """تعديل اسم/وصف/رقم واتساب متجر (المدير يُعيَّن من مسار منفصل)."""
    name = ""
    try:
        data = request.get_json(silent=True) or {}
        name = _safe_str(data.get('name'))
        description = _safe_str(data.get('description'))
        image_url = _safe_str(data.get('image_url'))
        whatsapp = normalize_whatsapp(data.get('whatsapp_number'))
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        if not name:
            return jsonify({"status": "error", "message": "اسم المتجر مطلوب"}), 400
        if not whatsapp:
            return jsonify({"status": "error", "message": "رقم واتساب المتجر مطلوب"}), 400
        if len(image_url) > 500:
            return jsonify({"status": "error", "message": "رابط صورة المتجر طويل جداً"}), 400
        store.name = name
        store.description = description or None
        store.image_url = image_url or None
        store.whatsapp_number = whatsapp
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حفظ تعديلات المتجر", "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل تعديل متجر: store_id=%s name=%s", store_id, name)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تعديل المتجر"}), 500

@app.route('/admin/remove-store', methods=['POST'])
@admin_required
def remove_store():
    """حذف متجر فارغ فقط — يُرفض إن كان فيه منتجات (نفس نمط المواد والكليات)."""
    store_id = None
    try:
        data = request.get_json(silent=True) or request.form or {}
        store_id = data.get('store_id')
        try:
            store_id = int(store_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرّف المتجر غير صحيح"}), 400
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        product_count = Product.query.filter_by(store_id=store_id).count()
        if product_count:
            return jsonify({"status": "error", "message": f"لا يمكن حذف متجر فيه منتجات — احذف المنتجات أولاً ({product_count} منتج)"}), 400
        manager_id = store.manager_id
        db.session.delete(store)
        db.session.commit()
        # إن لم يعد المدير يدير أي متجر آخر أعد دوره إلى طالب
        if manager_id:
            manager = Student.query.get(manager_id)
            if manager and manager.role == 'store_manager':
                remaining = Store.query.filter_by(manager_id=manager_id).count()
                if not remaining:
                    manager.role = 'student'
                    db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف المتجر بنجاح"})
    except Exception:
        app.logger.exception("فشل حذف متجر: store_id=%s", store_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حذف المتجر"}), 500

@app.route('/admin/pause-store/<int:store_id>', methods=['POST'])
@admin_required
def pause_store(store_id):
    """تبديل حالة متجر بين ظاهر وموقوف مؤقتاً."""
    try:
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        store.status = 'paused' if store.status == 'active' else 'active'
        db.session.commit()
        msg = "تم إيقاف المتجر مؤقتاً" if store.status == 'paused' else "تم إظهار المتجر من جديد"
        return jsonify({"status": "success", "message": msg, "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل تبديل حالة متجر: store_id=%s", store_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تغيير حالة المتجر"}), 500

@app.route('/admin/assign-store-manager', methods=['POST'])
@admin_required
def assign_store_manager():
    """تعيين مدير لمتجر من قائمة الطلاب، أو إزالته (student_id فارغ).
    عند التعيين يصبح الطالب store_manager، وعند إزالته من كل متاجره يعود student."""
    store_id = student_id = None
    try:
        data = request.get_json(silent=True) or {}
        store_id = data.get('store_id')
        student_id = data.get('student_id')
        try:
            store_id = int(store_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرّف المتجر غير صحيح"}), 400
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        previous_manager_id = store.manager_id
        new_manager = None
        if student_id:
            try:
                new_manager = Student.query.get(int(student_id))
            except (TypeError, ValueError):
                new_manager = None
            if not new_manager:
                return jsonify({"status": "error", "message": "الطالب المختار غير موجود"}), 400
            if new_manager.role == 'admin':
                return jsonify({"status": "error", "message": "هذا الحساب مشرف أعلى بالفعل"}), 400
        store.manager_id = new_manager.id if new_manager else None
        if new_manager:
            new_manager.role = 'store_manager'
        db.session.commit()
        if previous_manager_id and previous_manager_id != (new_manager.id if new_manager else None):
            prev = Student.query.get(previous_manager_id)
            if prev and prev.role == 'store_manager':
                remaining = Store.query.filter_by(manager_id=previous_manager_id).count()
                if not remaining:
                    prev.role = 'student'
                    db.session.commit()
        return jsonify({"status": "success", "message": "تم تعيين مدير المتجر بنجاح" if new_manager else "تمت إزالة مدير المتجر", "store": store.to_dict()})
    except Exception:
        app.logger.exception("فشل تعيين مدير متجر: store_id=%s student_id=%s", store_id, student_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تعيين المدير"}), 500

@app.route('/admin/students', methods=['GET'])
@admin_required
def get_students_for_admin():
    """كل الطلاب (id وusername وname والدور) لقائمة اختيار مدير المتجر المنسدلة."""
    try:
        students = Student.query.order_by(Student.name).all()
        return jsonify([{"id": s.id, "username": s.username, "name": s.name, "role": s.role or 'student'} for s in students])
    except Exception:
        app.logger.exception("فشل جلب قائمة الطلاب للإدارة")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/my-stores', methods=['GET'])
@store_manager_required
def get_my_stores():
    """متاجر مدير المتجر فقط (الأدمن يرى كل المتاجر من /admin/stores)."""
    try:
        user = get_current_user()
        if user.role == 'admin':
            stores = Store.query.order_by(Store.id).all()
        else:
            stores = Store.query.filter_by(manager_id=user.id).order_by(Store.id).all()
        return jsonify([s.to_dict() for s in stores])
    except Exception:
        app.logger.exception("فشل جلب متاجر المدير")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/my-stores/<int:store_id>/products', methods=['GET'])
@store_manager_required
def get_my_store_products(store_id):
    """منتجات متجر للمدير (حتى لو كان موقوفاً — لا تُعرض للعموم): مديره فقط أو الأدمن."""
    try:
        user = get_current_user()
        store = Store.query.get(store_id)
        if not store:
            return jsonify({"status": "error", "message": "المتجر غير موجود"}), 404
        if not _can_manage_store(user, store):
            return jsonify({"status": "error", "message": "هذا المتجر ليس من متاجرك"}), 403
        products = Product.query.filter_by(store_id=store_id).order_by(Product.id).all()
        return jsonify([p.to_dict() for p in products])
    except Exception:
        app.logger.exception("فشل جلب منتجات متجر المدير: store_id=%s", store_id)
        db.session.rollback()
        return jsonify([])

@app.route('/api/profile/update', methods=['POST'])
@student_required
def update_own_profile():
    """المستخدم يعدّل بياناته بنفسه: الاسم + اليوزر + كلمة السر.
    تغيير كلمة السر يتطلب الحالية + الجديدة + تأكيدها. لا يغيّر الدور ولا الحالة."""
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        new_name = _safe_str(data.get('name'))
        new_username = _safe_str(data.get('username'))
        current_password = str(data.get('current_password') or '')
        new_password = str(data.get('new_password') or '')
        confirm_password = str(data.get('confirm_password') or '')

        # التحقق كاملاً قبل أي تعديل حتى لا يُحفظ تعديل جزئي عند فشل الآخر.
        if new_password:
            if not current_password or not user.check_password(current_password):
                return jsonify({"status": "error", "message": "كلمة المرور الحالية غير صحيحة"}), 400
            if len(new_password) < 4:
                return jsonify({"status": "error", "message": "كلمة المرور الجديدة قصيرة جداً (4 أحرف على الأقل)"}), 400
            if new_password != confirm_password:
                return jsonify({"status": "error", "message": "تأكيد كلمة المرور غير مطابق"}), 400
        if new_username and new_username != user.username:
            exists = Student.query.filter(db.func.lower(Student.username) == new_username.lower()).first()
            if exists:
                return jsonify({"status": "error", "message": "اسم المستخدم محجوز — اختر اسماً آخر"}), 400

        if new_name:
            user.name = new_name
            session['student_name'] = new_name
        if new_username and new_username != user.username:
            user.username = new_username
        if new_password:
            user.set_password(new_password)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث بياناتك بنجاح", "student": user.to_dict()})
    except Exception as e:
        app.logger.exception("update_own_profile failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تحديث البيانات. حاول مرة أخرى."}), 500

# ==========================================
# إدارة الحسابات (المشرف الأعلى فقط) + نظام VIP
# ==========================================

@app.route('/admin/accounts', methods=['GET'])
@admin_required
def get_admin_accounts():
    """كل حسابات الموقع: اليوزر والاسم والدور والحالة وXP والمستوى وتاريخ
    التسجيل، مع بحث باليوزر أو الاسم (?q=)."""
    try:
        q = (request.args.get('q') or '').strip()
        query = Student.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Student.username.ilike(like), Student.name.ilike(like)))
        students = query.order_by(Student.created_at.desc()).all()
        # XP/المستوى من جدول واحد لكل الحسابات دفعة واحدة (لا استعلام
        # لكل طالب — كان البحث في تبويب VIP بطيئاً بسبب N+1).
        _ids = [s.id for s in students]
        _profiles = {p.student_id: p for p in UserProfile.query.filter(UserProfile.student_id.in_(_ids)).all()} if _ids else {}
        result = []
        for s in students:
            profile = _profiles.get(s.id)
            d = s.to_dict()
            d["xp"] = profile.points if profile else 0
            d["level"] = profile.level if profile else 1
            result.append(d)
        return jsonify(result)
    except Exception as e:
        app.logger.exception("get_admin_accounts failed")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/reset-password', methods=['POST'])
@admin_required
def admin_reset_password():
    """إعادة تعيين كلمة سر حساب (كلمة السر الحالية لا تُعرض أبداً لأنها
    مشفّرة ولا يمكن استرجاعها — تُستبدل بجديدة مشفّرة)."""
    try:
        data = request.get_json(silent=True) or {}
        try:
            student_id = int(data.get('student_id') or 0)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الحساب غير محدد"}), 400
        new_password = str(data.get('new_password') or '')
        if len(new_password) < 4:
            return jsonify({"status": "error", "message": "كلمة المرور الجديدة قصيرة جداً (4 أحرف على الأقل)"}), 400
        student = Student.query.get(student_id)
        if not student:
            return jsonify({"status": "error", "message": "الحساب غير موجود"}), 404
        student.set_password(new_password)
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم تعيين كلمة سر جديدة لحساب @{student.username}"})
    except Exception as e:
        app.logger.exception("admin_reset_password failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إعادة تعيين كلمة السر."}), 500

@app.route('/admin/ban-user', methods=['POST'])
@admin_required
def admin_ban_user():
    """تبديل حظر حساب (is_active). الأدمن لا يحظر نفسه."""
    admin = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        try:
            student_id = int(data.get('student_id') or 0)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الحساب غير محدد"}), 400
        student = Student.query.get(student_id)
        if not student:
            return jsonify({"status": "error", "message": "الحساب غير موجود"}), 404
        if student.id == admin.id:
            return jsonify({"status": "error", "message": "لا يمكنك حظر حسابك"}), 400
        student.is_active = not bool(student.is_active)
        db.session.commit()
        msg = "تم حظر الحساب" if not student.is_active else "تم فك حظر الحساب"
        return jsonify({"status": "success", "message": msg, "is_active": bool(student.is_active)})
    except Exception as e:
        app.logger.exception("admin_ban_user failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تبديل الحظر."}), 500

@app.route('/admin/delete-user', methods=['POST'])
@admin_required
def admin_delete_user():
    """حذف حساب نهائياً مع كل تابعيه، وحفظ اليوزر في شاهد قبر حتى تعرض
    محاولة دخول لاحقة رسالة الحذف. لا يحذف الأدمن نفسه ولا حساب a0mr1."""
    admin = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        try:
            student_id = int(data.get('student_id') or 0)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الحساب غير محدد"}), 400
        student = Student.query.get(student_id)
        if not student:
            return jsonify({"status": "error", "message": "الحساب غير موجود"}), 404
        if student.id == admin.id:
            return jsonify({"status": "error", "message": "لا يمكنك حذف حسابك"}), 400
        if student.username == 'a0mr1':
            return jsonify({"status": "error", "message": "لا يمكن حذف حساب المشرف الأعلى a0mr1"}), 400

        username = student.username
        # 1) فك ارتباط المتاجر التي يديرها (manager_id -> NULL).
        Store.query.filter_by(manager_id=student.id).update({"manager_id": None}, synchronize_session=False)
        # 2) منح VIP وآراء الدكاترة وأصواتها.
        VipGrant.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        _opinion_ids = [o.id for o in DoctorOpinion.query.filter_by(student_id=student.id).all()]
        if _opinion_ids:
            DoctorOpinionVote.query.filter(DoctorOpinionVote.opinion_id.in_(_opinion_ids)).delete(synchronize_session=False)
        DoctorOpinion.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        DoctorOpinionVote.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        # 3) السجلات المرتبطة بمفاتيح أجنبية على students.
        PointTransaction.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        UnifiedTask.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        Favorite.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        Progress.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        QuizAttempt.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        UserProfile.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        # أسئلة الدروس ومحتواه تبقى للمنصة بلا مالك (student_id -> NULL).
        LessonQuestion.query.filter_by(student_id=student.id).update({"student_id": None}, synchronize_session=False)
        LessonQuestion.query.filter_by(answered_by=student.id).update({"answered_by": None}, synchronize_session=False)
        CustomLesson.query.filter_by(created_by=student.id).update({"created_by": None}, synchronize_session=False)
        QuizQuestion.query.filter_by(created_by=student.id).update({"created_by": None}, synchronize_session=False)
        # 4) الحساب نفسه + شاهد القبر.
        db.session.delete(student)
        db.session.add(DeletedAccount(username=username, reason='حذف من المشرف الأعلى'))
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم حذف حساب @{username} نهائياً"})
    except Exception as e:
        app.logger.exception("admin_delete_user failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر حذف الحساب."}), 500

@app.route('/admin/vip-grants', methods=['GET'])
@admin_required
def get_admin_vip_grants():
    """كل منح VIP (أو منح مستخدم واحد بـ ?student_id=) مع أسماء الأهداف
    وحالة الصلاحية."""
    try:
        student_id = request.args.get('student_id')
        query = VipGrant.query
        if student_id:
            try:
                query = query.filter_by(student_id=int(student_id))
            except (TypeError, ValueError):
                return jsonify([])
        grants = query.order_by(VipGrant.created_at.desc()).all()
        return jsonify([_serialize_vip_grant(g) for g in grants])
    except Exception as e:
        app.logger.exception("get_admin_vip_grants failed")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/vip-content', methods=['GET'])
@admin_required
def get_admin_vip_content():
    """المحتوى المعلَّم VIP (لاختيار هدف منح محتوى)."""
    try:
        lessons = CustomLesson.query.filter_by(is_vip=True).order_by(CustomLesson.subject_name, CustomLesson.chapter_name).all()
        return jsonify([{"id": l.id, "title": l.title, "subject_name": l.subject_name, "chapter_name": l.chapter_name, "content_type": l.content_type} for l in lessons])
    except Exception as e:
        app.logger.exception("get_admin_vip_content failed")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/vip-grant', methods=['POST'])
@admin_required
def admin_vip_grant():
    """منح VIP لمستخدم: مادة / محتوى / خدمة، لمدة (أيام/شهر/سنة/أبدي)."""
    admin = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        try:
            student_id = int(data.get('student_id') or 0)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "المستخدم غير محدد"}), 400
        scope_type = str(data.get('scope_type') or '').strip()
        scope_key = str(data.get('scope_key') or '').strip()
        duration = str(data.get('duration') or '').strip()
        if scope_type not in ('subject', 'content', 'service'):
            return jsonify({"status": "error", "message": "نوع الهدف غير صالح"}), 400
        if not scope_key:
            return jsonify({"status": "error", "message": "الهدف غير محدد"}), 400
        student = Student.query.get(student_id)
        if not student:
            return jsonify({"status": "error", "message": "المستخدم غير موجود"}), 404
        # التحقق من وجود الهدف.
        if scope_type == 'subject':
            try:
                if not Subject.query.get(int(scope_key)):
                    return jsonify({"status": "error", "message": "المادة غير موجودة"}), 400
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "المادة غير موجودة"}), 400
        elif scope_type == 'content':
            try:
                if not CustomLesson.query.get(int(scope_key)):
                    return jsonify({"status": "error", "message": "المحتوى غير موجود"}), 400
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "المحتوى غير موجود"}), 400
        elif scope_type == 'service':
            if scope_key not in VIP_SERVICE_KEYS:
                return jsonify({"status": "error", "message": "الخدمة غير موجودة"}), 400
        # حساب الانتهاء.
        expires_at = None
        if duration == 'forever':
            expires_at = None
        elif duration == 'month':
            expires_at = datetime.utcnow() + timedelta(days=30)
        elif duration == 'year':
            expires_at = datetime.utcnow() + timedelta(days=365)
        elif duration == 'days':
            try:
                days = int(data.get('days') or 0)
            except (TypeError, ValueError):
                days = 0
            if days < 1 or days > 3650:
                return jsonify({"status": "error", "message": "عدد الأيام يجب أن يكون بين 1 و 3650"}), 400
            expires_at = datetime.utcnow() + timedelta(days=days)
        else:
            return jsonify({"status": "error", "message": "المدة غير صالحة"}), 400
        # منح مكرر نشط لنفس الهدف: جدّده بدل إنشاء صف مكرر.
        existing = VipGrant.query.filter_by(student_id=student_id, scope_type=scope_type, scope_key=scope_key).all()
        for g in existing:
            if _vip_grant_is_active(g):
                g.expires_at = expires_at
                g.granted_by = admin.id
                db.session.commit()
                return jsonify({"status": "success", "message": "تم تجديد المنح", "grant": _serialize_vip_grant(g)})
        grant = VipGrant(student_id=student_id, scope_type=scope_type, scope_key=scope_key, expires_at=expires_at, granted_by=admin.id)
        db.session.add(grant)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم منح VIP بنجاح", "grant": _serialize_vip_grant(grant)})
    except Exception as e:
        app.logger.exception("admin_vip_grant failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر منح VIP."}), 500

@app.route('/admin/vip-revoke', methods=['POST'])
@admin_required
def admin_vip_revoke():
    """إلغاء منح VIP."""
    try:
        data = request.get_json(silent=True) or {}
        try:
            grant_id = int(data.get('grant_id') or 0)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "المنح غير محدد"}), 400
        grant = VipGrant.query.get(grant_id)
        if not grant:
            return jsonify({"status": "error", "message": "المنح غير موجود"}), 404
        db.session.delete(grant)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إلغاء المنح"})
    except Exception as e:
        app.logger.exception("admin_vip_revoke failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إلغاء المنح."}), 500

@app.route('/admin/vip-services', methods=['GET'])
@admin_required
def get_admin_vip_services():
    """حالة إغلاق خدمات الفقاعة الخمس كـ VIP."""
    try:
        rows = {r.service_key: bool(r.is_vip) for r in VipService.query.all()}
        return jsonify({k: rows.get(k, False) for k in VIP_SERVICE_KEY_LIST})
    except Exception as e:
        app.logger.exception("get_admin_vip_services failed")
        db.session.rollback()
        return jsonify({k: False for k in VIP_SERVICE_KEY_LIST})

@app.route('/admin/vip-service', methods=['POST'])
@admin_required
def admin_vip_service():
    """تشغيل/إيقاف إغلاق خدمة كـ VIP. عند الإيقاف تعود مجانية للجميع فوراً."""
    try:
        data = request.get_json(silent=True) or {}
        key = str(data.get('service_key') or '').strip()
        if key not in VIP_SERVICE_KEYS:
            return jsonify({"status": "error", "message": "الخدمة غير موجودة"}), 400
        is_vip = _is_truthy(data.get('is_vip'))
        row = VipService.query.get(key)
        if not row:
            row = VipService(service_key=key, is_vip=is_vip)
            db.session.add(row)
        else:
            row.is_vip = is_vip
        db.session.commit()
        msg = f"تم إغلاق «{VIP_SERVICE_KEYS[key]}» كـ VIP" if is_vip else f"عادت «{VIP_SERVICE_KEYS[key]}» مجانية للجميع"
        return jsonify({"status": "success", "message": msg, "is_vip": is_vip})
    except Exception as e:
        app.logger.exception("admin_vip_service failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر تحديث الخدمة."}), 500

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
        
        discipline = (data.get('discipline') or 'متوازن').strip()
        wird_pages = data.get('wird_pages', 1)
        wird_enabled = data.get('wird', True)
        
        base_profiles = {
            "مرن": "1 to 2 hours total per day. Maximum 1-2 small study tasks per active day.",
            "متوازن": "2 to 3.5 hours total per day. Maximum 2 moderate tasks per active day.",
            "مكثف": "4 to 5.5 hours total per day. Maximum 3 focused tasks per active day."
        }
        
        limit_desc = base_profiles.get(discipline, base_profiles["متوازن"])
        wird_text = f"Daily Wird: {wird_pages} pages every day (0.5 hour allocated)." if wird_enabled and wird_pages > 0 else "No daily Wird."

        system_instruction = (
            "You are a realistic academic study planner. DO NOT overwhelm the student.\n"
            + f"Workload Limit: {limit_desc}\n"
            + f"{wird_text}\n"
            + "STRICT RULES:\n"
            + "1. Output ONLY valid JSON: {'plans': [{'day_name': 'السبت', 'date': 'YYYY-MM-DD', 'is_off_day': false, 'tasks': [{'title': 'مهمة واقعية ومحددة', 'allocated_hours': 1.5}]}]}\n"
            + "2. Total daily study hours must NEVER exceed the limit specified above.\n"
            + "3. Only give 1 or 2 high-value distinct subjects per day. Do not repeat identical task names over and over.\n"
            + "4. Off days must have 'is_off_day': true and 'tasks': []."
        )

        user_prompt = (
            f"Start Date: {data.get('current_date')}\n"
            + f"Target Date: {data.get('target_date')}\n"
            + f"Subjects: {data.get('subjects')}\n"
            + f"Mode: {discipline}\n"
            + f"Weekly Off-Days: {', '.join(data.get('off_days', []))}\n"
            + f"Quran Wird: {'Yes, ' + str(wird_pages) + ' pages' if wird_enabled and wird_pages > 0 else 'No'}"
        )

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.4
        )

        plan_data = json.loads(response.choices[0].message.content)

        if student_id and plan_data.get('plans'):
            try:
                UnifiedTask.query.filter_by(student_id=student_id, source='planner').delete()
                
                for day in plan_data['plans']:
                    day_date = day.get('date')
                    if not day_date:
                        continue
                    try:
                        planned_date = datetime.strptime(day_date, '%Y-%m-%d').date()
                    except ValueError:
                        continue
                    for task in day.get('tasks', []):
                        task_title = task.get('title', '').strip()
                        if not task_title:
                            continue
                        allocated_hours = float(task.get('allocated_hours', 1.0))
                        is_wird = 'الورد' in task_title or 'قراءة' in task_title
                        
                        unified_task = UnifiedTask(
                            student_id=student_id,
                            lesson_id=None,
                            title=task_title,
                            planned_date=planned_date,
                            source='planner',
                            priority=2,
                            allocated_hours=allocated_hours,
                            is_wird=is_wird,
                            completed=False
                        )
                        db.session.add(unified_task)
                db.session.commit()
                return jsonify({"status": "success", "plan": plan_data})
            except Exception:
                db.session.rollback()
                app.logger.exception("generate_plan: failed to save generated tasks")
                return jsonify({"error": "تعذر حفظ الخطة، حاول لاحقاً"}), 500

        return jsonify({"status": "success", "plan": plan_data})

    except Exception:
        app.logger.exception("generate_plan: failed")
        return jsonify({"error": AI_UNAVAILABLE_MSG}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
            
        missed = data.get('missed_tasks', [])
        remaining = data.get('remaining_plans', [])

        system_instruction = (
            "You are an academic rescheduling assistant. Re-distribute uncompleted tasks smartly into the remaining days without overloading the student. Max 2-3 hours daily."
        )

        user_prompt = (
            f"Missed Tasks: {json.dumps(missed, ensure_ascii=False)}\n"
            + f"Remaining Schedule: {json.dumps(remaining, ensure_ascii=False)}"
        )

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.4
        )

        reply_text = response.choices[0].message.content
        plan_data = json.loads(reply_text)
        return jsonify({"status": "success", "plan": plan_data})
    except Exception:
        app.logger.exception("reschedule_plan: failed")
        return jsonify({"error": AI_UNAVAILABLE_MSG}), 500

@app.route('/api/tasks/complete/<int:task_id>', methods=['POST'])
@student_required
def complete_task(task_id):
    try:
        student_id = session['student_id']
        # ids outside Postgres INTEGER range (e.g. a frontend Date.now()
        # temp id) can never be a real row — answer 404 JSON instead of
        # letting the lookup raise 22003.
        if not (-(2 ** 31) <= task_id <= 2 ** 31 - 1):
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != student_id:
            return jsonify({"status": "error", "message": "غير مصرح لك"}), 403
        
        if task.completed:
            return jsonify({"status": "error", "message": "المهمة مكتملة بالفعل"}), 400
        
        task.completed = True
        db.session.commit()
        
        profile = UserProfile.query.filter_by(student_id=student_id).first()
        points_earned = 10
        reason = f"إكمال مهمة: {task.title}"
        if task.is_wird:
            points_earned = 5
            reason = f"إنجاز الورد القرآني: {task.title}"
        
        result = profile.add_points(points_earned, reason) if profile else None
        
        return jsonify({
            "status": "success",
            "message": "تم إكمال المهمة بنجاح!",
            "points_earned": points_earned,
            "profile": profile.to_dict() if profile else None
        })
    except Exception as e:
        db.session.rollback()
        app.logger.exception("complete_task: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks', methods=['GET', 'POST', 'DELETE'])
@student_required
def manage_unified_tasks():
    student_id = session.get('student_id')
    try:
        if request.method == 'GET':
            source = request.args.get('source')
            query = UnifiedTask.query.filter_by(student_id=student_id)
            if source:
                query = query.filter_by(source=source)
            tasks = query.order_by(UnifiedTask.planned_date).all()
            return jsonify([t.to_dict() for t in tasks])

        if request.method == 'DELETE':
            # Bulk clear-all: delete every task of this student in one
            # transaction. The old frontend deleted tasks one by one
            # (24+ sequential DELETEs, ~75s on the free tier) with no
            # visible progress, so users re-clicked or reloaded mid-loop
            # and tasks survived; overlapping loops also raced each other
            # (same id DELETE'd 2-3 times, 200 then 404).
            deleted = UnifiedTask.query.filter_by(student_id=student_id).delete(synchronize_session=False)
            db.session.commit()
            return jsonify({"status": "success", "deleted": deleted})

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        planned_date = data.get('planned_date')
        source = data.get('source', 'manual')
        priority = data.get('priority', 2)
        lesson_id = data.get('lesson_id')
        description = data.get('description', '')
        allocated_hours = data.get('allocated_hours', 1.0)
        is_wird = data.get('is_wird', False)

        # Idempotency guard (dedupe by unique id): the planner re-syncs
        # unsynced local tasks; if the task id already exists for this
        # student, return the existing row instead of creating a duplicate.
        # NOTE: the frontend creates new tasks with a local temp id of
        # Date.now() (13 digits, e.g. 1795367806512) which is far outside
        # Postgres INTEGER range (max 2147483647). Binding it as INTEGER
        # raises 22003, so the task was never saved and every later PUT /
        # DELETE / complete on that id failed too. An out-of-range id can
        # never be a real row: skip the lookup and create the task, and
        # the frontend swaps in the real server id from the response.
        existing_id = data.get('id')
        try:
            existing_id = int(existing_id) if existing_id is not None else None
        except (TypeError, ValueError):
            existing_id = None
        if existing_id and -(2 ** 31) <= existing_id <= 2 ** 31 - 1:
            existing = UnifiedTask.query.filter_by(id=existing_id, student_id=student_id).first()
            if existing:
                return jsonify({"status": "success", "task": existing.to_dict()})

        if not title or not planned_date:
            return jsonify({"status": "error", "message": "العنوان والتاريخ مطلوبان"}), 400

        try:
            planned_date_obj = datetime.strptime(planned_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

        task = UnifiedTask(
            student_id=student_id,
            lesson_id=lesson_id,
            title=title,
            description=description,
            planned_date=planned_date_obj,
            source=source,
            priority=priority,
            allocated_hours=allocated_hours,
            is_wird=is_wird,
            completed=False
        )
        db.session.add(task)
        db.session.commit()
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("manage_unified_tasks: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/<int:task_id>', methods=['PUT', 'DELETE'])
@student_required
def handle_single_task(task_id):
    student_id = session['student_id']
    # ids outside Postgres INTEGER range (e.g. a frontend Date.now()
    # temp id) can never be a real row — answer 404 JSON instead of
    # letting the lookup raise 22003 as an uncaught HTML 500, and keep
    # the lookup inside the try so any DB error returns JSON with a
    # logged traceback instead of a bare failure page.
    if not (-(2 ** 31) <= task_id <= 2 ** 31 - 1):
        return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404

    try:
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != student_id:
            return jsonify({"status": "error", "message": "غير مصرح لك"}), 403

        if request.method == 'DELETE':
            db.session.delete(task)
            db.session.commit()
            return jsonify({"status": "success", "message": "تم حذف المهمة"})

        data = request.get_json(silent=True) or {}
        if 'completed' in data:
            is_completed = bool(data['completed'])
            if is_completed and not task.completed:
                profile = UserProfile.query.filter_by(student_id=student_id).first()
                if profile:
                    points = 5 if task.is_wird else 10
                    profile.add_points(points, f"إكمال مهمة: {task.title}")
            task.completed = is_completed

        if 'planned_date' in data:
            try:
                task.planned_date = datetime.strptime(data['planned_date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400
        if 'title' in data:
            task.title = data['title'].strip()
        if 'description' in data:
            task.description = data['description'].strip()
        if 'priority' in data:
            task.priority = int(data['priority'])
        if 'allocated_hours' in data:
            task.allocated_hours = float(data['allocated_hours'])
        if 'is_wird' in data:
            task.is_wird = bool(data['is_wird'])

        task.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("handle_single_task: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/lessons', methods=['GET'])
@student_required
def get_all_lessons():
    user = get_current_user()
    try:
        lessons = CustomLesson.query.filter_by(status='approved').order_by(CustomLesson.subject_name, CustomLesson.chapter_name).all()
        result = []
        for l in lessons:
            result.append({
                "id": l.id,
                "title": l.title,
                "subject_name": l.subject_name,
                "subject_id": l.subject_id,
                "chapter_name": l.chapter_name,
                "content_type": l.content_type,
                "is_vip": bool(l.is_vip),
                "vip_locked": _lesson_vip_locked(user, l),
                "year": display_year(l.year),
                "term": l.term,
                "college": l.college or "كلية الهندسة"
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/generate-exam-plan', methods=['POST'])
@student_required
def generate_exam_plan():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        student_id = session['student_id']
        plan_name = (data.get('plan_name') or '').strip()
        exam_date = data.get('exam_date')
        lesson_ids = data.get('lesson_ids', [])

        if not plan_name or not exam_date or not lesson_ids:
            return jsonify({"status": "error", "message": "اسم الاختبار والتاريخ والدروس مطلوبة"}), 400

        try:
            exam_date_obj = datetime.strptime(exam_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

        lessons = CustomLesson.query.filter(CustomLesson.id.in_(lesson_ids)).all()
        if not lessons:
            return jsonify({"status": "error", "message": "لم يتم العثور على الدروس المحددة"}), 404

        lessons_info = [{"id": l.id, "title": l.title, "type": l.content_type} for l in lessons]
        today = datetime.now().date()
        delta = (exam_date_obj - today).days
        if delta < 1:
            return jsonify({"status": "error", "message": "تاريخ الاختبار يجب أن يكون في المستقبل"}), 400

        last_study_day = exam_date_obj - timedelta(days=1)
        prompt = f"""
        أنت مخطط أكاديمي ذكي. وزع الدروس التالية على {delta} يوماً باعتدال وبدون إرهاق (1-2 درس يومياً بحد أقصى ساعة ونصف يومياً).
        تاريخ اليوم: {today.isoformat()}، وتاريخ الاختبار: {exam_date_obj.isoformat()}.
        أيام المذاكرة هي من {today.isoformat()} إلى {last_study_day.isoformat()} فقط — يجب أن يكون حقل "day" في كل يوم أحد هذه التواريخ، ولا تستخدم أي تاريخ آخر.
        الدروس: {json.dumps(lessons_info, ensure_ascii=False)}
        أخرج فقط JSON: {{"plan": [{{"day": "{today.isoformat()}", "tasks": [{{"lesson_id": 1, "title": "مقدمة", "priority": "متوسط", "allocated_hours": 1.0}}]}}]}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "أنت مخطط أكاديمي. أخرج فقط JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.4
        )

        plan_data = json.loads(response.choices[0].message.content)
        priority_map = {"عال": 1, "متوسط": 2, "منخفض": 3}

        # Never trust the model's dates: derive them deterministically from
        # the exam date. Study days run from today through the day before
        # the exam (exactly `delta` days), so tasks can never land in the
        # past or on/after the exam. The model's day order is kept; only
        # the calendar dates are replaced (and written back into the
        # returned plan so the response matches what was stored).
        study_days = [today + timedelta(days=i) for i in range(delta)]
        plan_days = plan_data.get('plan') or []
        if not isinstance(plan_days, list):
            plan_days = []
        for day_index, day_obj in enumerate(plan_days):
            day_date = study_days[min(day_index, len(study_days) - 1)]
            day_obj['day'] = day_date.isoformat()

            for task in day_obj.get('tasks', []):
                lesson_id = task.get('lesson_id')
                lesson = CustomLesson.query.get(lesson_id) if lesson_id else None
                unified_task = UnifiedTask(
                    student_id=student_id,
                    lesson_id=lesson_id,
                    title=task.get('title', lesson.title if lesson else 'مراجعة'),
                    description=f"خطة اختبار: {plan_name}",
                    planned_date=day_date,
                    source='exam',
                    priority=priority_map.get(task.get('priority', 'متوسط'), 2),
                    allocated_hours=float(task.get('allocated_hours', 1.0)),
                    is_wird=False,
                    completed=False
                )
                db.session.add(unified_task)
        db.session.commit()

        return jsonify({"status": "success", "plan": plan_data})

    except Exception:
        db.session.rollback()
        app.logger.exception("generate_exam_plan: failed")
        return jsonify({"status": "error", "message": AI_UNAVAILABLE_MSG}), 500

# ==========================================
# الملف الشخصي ولوحة المتصدرين والتقدم
# ==========================================

@app.route('/api/profile', methods=['GET'])
@student_required
def get_profile():
    try:
        student_id = session['student_id']
        student = Student.query.get(student_id)
        profile = UserProfile.query.filter_by(student_id=student_id).first()

        if not profile:
            profile = UserProfile(student_id=student_id)
            db.session.add(profile)
            db.session.commit()

        # الدروس المكتملة تُحتسب ضمن المهام: إكمال درس لا ينشئ UnifiedTask،
        # فكان عدّاد «مهام مكتملة» ونسبة الإنجاز يبقيان صفراً رغم إكمال الدروس.
        completed_lessons = Progress.query.filter_by(student_id=student_id, progress_percent=100).count()
        # تُحتسب فقط الدروس التي بدأها الطالب فعلاً (progress_percent > 0):
        # صفوف 0% كانت تُنشأ عند حفظ حالة أي درس آخر في الصفحة ولا تعني مهمة
        # بدأها الطالب، فكان تضمينها يضخّم «إجمالي المهام» ويهبط بنسبة الإنجاز.
        total_lessons = Progress.query.filter(Progress.student_id == student_id, Progress.progress_percent > 0).count()
        completed_tasks = UnifiedTask.query.filter_by(student_id=student_id, completed=True).count() + completed_lessons
        total_tasks = UnifiedTask.query.filter_by(student_id=student_id).count() + total_lessons
        recent_transactions = PointTransaction.query.filter_by(student_id=student_id).order_by(PointTransaction.created_at.desc()).limit(10).all()
        vip_grants = [_serialize_vip_grant(g) for g in VipGrant.query.filter_by(student_id=student_id).order_by(VipGrant.created_at.desc()).all()]

        return jsonify({
            "status": "success",
            "profile": profile.to_dict(),
            "student": student.to_dict(),
            "vip_grants": vip_grants,
            "stats": {
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "completion_rate": round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0, 1)
            },
            "recent_transactions": [t.to_dict() for t in recent_transactions]
        })
    except Exception as e:
        app.logger.exception("get_profile: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/profile/visibility', methods=['POST'])
@student_required
def update_profile_visibility():
    try:
        data = request.get_json(silent=True) or {}
        show = data.get('show', True)
        profile = UserProfile.query.filter_by(student_id=session['student_id']).first()
        if profile:
            profile.show_in_leaderboard = bool(show)
            db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    try:
        profiles = UserProfile.query.filter_by(show_in_leaderboard=True).order_by(UserProfile.points.desc()).limit(50).all()
        result = []
        rank = 1
        for p in profiles:
            student = Student.query.get(p.student_id)
            if student:
                result.append({
                    "rank": rank,
                    "student_id": p.student_id,
                    "name": student.name,
                    "username": student.username,
                    "level": p.level,
                    "points": p.points,
                    "streak_days": p.streak_days
                })
                rank += 1
        return jsonify({"status": "success", "leaderboard": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/favorites', methods=['GET', 'POST', 'DELETE'])
@student_required
def manage_favorites():
    try:
        student_id = session['student_id']
        if request.method == 'GET':
            favs = Favorite.query.filter_by(student_id=student_id).order_by(Favorite.added_at.desc()).all()
            return jsonify([f.to_dict() for f in favs])

        data = request.get_json(silent=True) or {}
        lesson_id = data.get('lesson_id')
        if not lesson_id:
            return jsonify({"status": "error", "message": "معرف الدرس مطلوب"}), 400
        try:
            lesson_id = int(lesson_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرف الدرس غير صالح"}), 400

        fav = Favorite.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()

        if request.method == 'POST':
            if not fav:
                db.session.add(Favorite(student_id=student_id, lesson_id=lesson_id))
                db.session.commit()
            return jsonify({"status": "success"})

        elif request.method == 'DELETE':
            if fav:
                db.session.delete(fav)
                db.session.commit()
            return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/progress', methods=['GET', 'POST'])
@student_required
def manage_progress():
    student_id = session['student_id']
    try:
        if request.method == 'GET':
            return jsonify([p.to_dict() for p in Progress.query.filter_by(student_id=student_id).all()])

        data = request.get_json(silent=True) or {}
        lesson_id = data.get('lesson_id')
        status = data.get('status', 'not_started')
        progress_percent = data.get('progress_percent', 0)
        try:
            lesson_id = int(lesson_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرف الدرس غير صالح"}), 400

        if type(progress_percent) is not int or not 0 <= progress_percent <= 100 or not db.session.get(CustomLesson, lesson_id):
            return jsonify({"status":"error","message":"بيانات الدرس غير صالحة"}), 400
        prog = Progress.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()
        if not prog:
            # لا ننشئ صفاً لدرس لم يبدأه الطالب (0%): صفوف 0% كانت تُحتسب
            # ضمن «إجمالي المهام» في الملف الشخصي فتضخّم المقام دون إنجاز.
            if progress_percent == 0:
                return jsonify({"status": "success"})
            prog = Progress(student_id=student_id, lesson_id=lesson_id)
            db.session.add(prog)

        old_progress = prog.progress_percent or 0
        prog.status = status
        prog.progress_percent = progress_percent
        prog.last_accessed = datetime.utcnow()
        db.session.commit()

        if prog.progress_percent == 100 and old_progress < 100:
            profile = UserProfile.query.filter_by(student_id=student_id).first()
            if profile:
                lesson = CustomLesson.query.get(lesson_id)
                lesson_title = lesson.title if lesson else "درس"
                profile.add_points(10, f"إكمال الدرس: {lesson_title}")

        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/updates', methods=['GET'])
def get_platform_updates():
    try:
        return jsonify([u.to_dict() for u in PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_platform_update():
    try:
        data = request.get_json(silent=True) or request.form or {}
        title = data.get('title', '').strip()
        tag = data.get('tag', 'هام').strip()
        description = data.get('description', '').strip()

        if not title or not description:
            return jsonify({"status": "error", "message": "العنوان والتفاصيل مطلوبة"}), 400

        db.session.add(PlatformUpdate(title=title, tag=tag, description=description))
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الإعلان بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health')
def health_check():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db.session.rollback()
        error = getattr(exc, "orig", exc)
        causes = [error]
        while causes[-1].__cause__ and causes[-1].__cause__ not in causes:
            causes.append(causes[-1].__cause__)
        tls_error = next((cause for cause in causes if isinstance(cause, ssl.SSLCertVerificationError)), None)
        details = error.args[0] if error.args else None
        sqlstate = details.get("C", "") if isinstance(details, dict) else ""
        sqlstate = sqlstate if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isalnum() else "-"
        app.logger.warning(
            "DB health failed: driver=%s errors=%s sqlstate=%s tls_verify_code=%s",
            db.engine.url.drivername,
            ",".join(type(cause).__name__ for cause in causes),
            sqlstate,
            tls_error.verify_code if tls_error else "-",
        )
        return jsonify(status="unavailable", database="disconnected"), 503
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected" if db.engine else "disconnected"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)



