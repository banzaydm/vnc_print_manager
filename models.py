from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default="#3498db")  # Hex цвет
    parent_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    
    servers = db.relationship('Server', backref='group', lazy=True)
    printers = db.relationship('Printer', backref='group', lazy=True)
    
    def __repr__(self):
        return f'<Group {self.name}>'

class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(15), unique=True, nullable=False)
    port = db.Column(db.Integer, default=5900)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    is_favorite = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, nullable=True)
    comment = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Server {self.name} ({self.ip})>'

class Printer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(15), unique=True, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    web_interface = db.Column(db.String(500), default='')
    status = db.Column(db.Boolean, default=False)
    comment = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Printer {self.name} ({self.ip})>'