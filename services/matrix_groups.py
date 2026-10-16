# -*- coding: utf-8 -*-
"""Локальные данные вкладки Matrix: группы пользователей и скрытие.

Группы и скрытые пользователи хранятся в БД приложения (не на
Matrix-сервере): они нужны только для фильтрации во вкладке. Серверное
удаление пользователей на части сборок Synapse недоступно (405), поэтому
«удаление из списка» реализовано как локальное скрытие.
"""
from models import MatrixGroup, MatrixGroupMember, MatrixHiddenUser, db

MAX_NAME = 64


class GroupError(Exception):
    """Ошибка валидации/поиска группы. http_code — статус для клиента."""

    def __init__(self, message, http_code=400):
        super().__init__(message)
        self.http_code = http_code


def _norm_name(name):
    name = (name or '').strip()
    if not name:
        raise GroupError('Название группы не может быть пустым')
    if len(name) > MAX_NAME:
        raise GroupError(
            f'Название группы — не длиннее {MAX_NAME} символов')
    return name


def _unique_name(name, exclude_id=None):
    q = MatrixGroup.query.filter_by(name=name)
    if exclude_id is not None:
        q = q.filter(MatrixGroup.id != exclude_id)
    if q.first() is not None:
        raise GroupError(f'Группа «{name}» уже существует', 409)


def _norm_user_id(user_id):
    user_id = (user_id or '').strip()
    if not user_id.startswith('@') or ':' not in user_id:
        raise GroupError('Некорректный user_id')
    return user_id


def list_groups():
    """Группы с участниками: [{id, name, count, members}]."""
    groups = MatrixGroup.query.order_by(MatrixGroup.name).all()
    by_group = {g.id: [] for g in groups}
    for m in MatrixGroupMember.query.all():
        if m.group_id in by_group:
            by_group[m.group_id].append(m.user_id)
    return [{
        'id': g.id,
        'name': g.name,
        'count': len(by_group[g.id]),
        'members': sorted(by_group[g.id]),
    } for g in groups]


def create_group(name):
    name = _norm_name(name)
    _unique_name(name)
    group = MatrixGroup(name=name)
    db.session.add(group)
    db.session.commit()
    return {'id': group.id, 'name': group.name}


def rename_group(group_id, name):
    group = db.session.get(MatrixGroup, group_id)
    if group is None:
        raise GroupError('Группа не найдена', 404)
    name = _norm_name(name)
    _unique_name(name, exclude_id=group.id)
    group.name = name
    db.session.commit()
    return {'id': group.id, 'name': group.name}


def delete_group(group_id):
    group = db.session.get(MatrixGroup, group_id)
    if group is None:
        raise GroupError('Группа не найдена', 404)
    MatrixGroupMember.query.filter_by(group_id=group.id).delete()
    db.session.delete(group)
    db.session.commit()
    return True


def assign(user_id, group_id):
    """Положить пользователя в группу или убрать (group_id None/пусто)."""
    user_id = _norm_user_id(user_id)
    if group_id in (None, '', 0, '0'):
        MatrixGroupMember.query.filter_by(user_id=user_id).delete()
        db.session.commit()
        return {'user_id': user_id, 'group_id': None}
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        raise GroupError('Некорректная группа')
    if db.session.get(MatrixGroup, group_id) is None:
        raise GroupError('Группа не найдена', 404)
    member = db.session.get(MatrixGroupMember, user_id)
    if member is not None:
        member.group_id = group_id
    else:
        db.session.add(MatrixGroupMember(user_id=user_id, group_id=group_id))
    db.session.commit()
    return {'user_id': user_id, 'group_id': group_id}


def list_hidden():
    """Скрытые («удалённые из списка») пользователи: [user_id]."""
    return [m.user_id for m in MatrixHiddenUser.query.all()]


def set_hidden(user_id, hidden):
    """Скрыть пользователя из вкладки или вернуть в список."""
    user_id = _norm_user_id(user_id)
    row = db.session.get(MatrixHiddenUser, user_id)
    if hidden:
        if row is None:
            db.session.add(MatrixHiddenUser(user_id=user_id))
    elif row is not None:
        db.session.delete(row)
    db.session.commit()
    return {'user_id': user_id, 'hidden': bool(hidden)}
