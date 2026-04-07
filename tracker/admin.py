from django.contrib import admin
from .models import Student, TestMark, Subject, TeacherCredential

admin.site.register(Student)
admin.site.register(TestMark)
admin.site.register(Subject)
admin.site.register(TeacherCredential)