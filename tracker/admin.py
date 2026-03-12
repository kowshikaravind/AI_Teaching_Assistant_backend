from django.contrib import admin
from .models import Student, TestMark, Attendance, Subject

admin.site.register(Student)
admin.site.register(TestMark)
admin.site.register(Attendance)
admin.site.register(Subject)