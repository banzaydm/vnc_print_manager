from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone

db = SQLAlchemy()


def utcnow():
    """Текущее время UTC (naive), совместимое с SQLAlchemy DateTime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default="#3498db")  # Hex цвет
    parent_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)

    servers = db.relationship('Server', backref='group', lazy=True)
    printers = db.relationship('Printer', backref='group', lazy=True)
    cameras = db.relationship('Camera', backref='group', lazy=True)
    routers = db.relationship('Router', backref='group', lazy=True)

    def __repr__(self):
        return f'<Group {self.name}>'


class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), unique=True, nullable=False)
    port = db.Column(db.Integer, default=5900)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    is_favorite = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, nullable=True)
    comment = db.Column(db.Text, default='')
    rustdesk_id = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Server {self.name} ({self.ip})>'


class Printer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), unique=True, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    web_interface = db.Column(db.String(500), default='')
    is_favorite = db.Column(db.Boolean, default=False)
    status = db.Column(db.Boolean, default=False)
    comment = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Printer {self.name} ({self.ip})>'


class Camera(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), unique=True, nullable=False)
    port = db.Column(db.Integer, default=80)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    web_interface = db.Column(db.String(500), default='')
    rtsp_url = db.Column(db.String(500), default='')
    username = db.Column(db.String(100), default='')
    password = db.Column(db.String(200), default='')
    is_favorite = db.Column(db.Boolean, default=False)
    status = db.Column(db.Boolean, default=False)
    comment = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Camera {self.name} ({self.ip})>'


class Router(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), unique=True, nullable=False)
    port = db.Column(db.Integer, default=80)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    web_interface = db.Column(db.String(500), default='')
    username = db.Column(db.String(100), default='')
    password = db.Column(db.String(200), default='')
    is_favorite = db.Column(db.Boolean, default=False)
    status = db.Column(db.Boolean, default=False)
    comment = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Router {self.name} ({self.ip})>'


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # admin / user
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    type = db.Column(db.String(20), default='string')  # string, json, boolean, number
    description = db.Column(db.String(200), nullable=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f'<Settings {self.key}={self.value}>'


class SubnetName(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subnet = db.Column(db.String(18), unique=True, nullable=False)  # Например: 192.168.1.0/24
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f'<SubnetName {self.subnet}={self.name}>'