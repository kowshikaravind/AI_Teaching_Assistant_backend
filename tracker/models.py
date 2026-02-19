from django.db import models

# Create your models here.
class Student(models.Model):
    roll_number = models.CharField(max_length = 20, unique = True)
    name = models.CharField(max_length=100)
    parent_number = models.CharField(max_length=15,default="")
    class_name = models.CharField(max_length=5)
    def __str__(self):
        return self.name
class TestMark(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    subject = models.CharField(max_length=50 , default="General")
    test_name = models.CharField(max_length=100)
    marks_obtained = models.FloatField()
    total_marks = models.FloatField()
    date_taken = models.DateField()
    def __str__(self):
        return f"{self.student.name} - {self.test_name}"