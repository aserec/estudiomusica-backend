"""Tipos de permiso y rol de curso. Los slugs de permiso siguen el frontend."""

from enum import StrEnum


class Permission(StrEnum):
    """Permisos versionados. El recurso y la acción van separados por ``:``."""

    COURSE_VIEW = "course:view"
    COURSE_GRADE = "course:grade"
    COURSE_TEACH = "course:teach"
    SITE_MANAGE_ADMINS = "site:manage-admins"
    SITE_MANAGE_COURSES = "site:manage-courses"


class CourseRole(StrEnum):
    """Rol de un usuario dentro de un curso concreto."""

    STUDENT = "student"
    ASSISTANT = "assistant"
    TEACHER = "teacher"
