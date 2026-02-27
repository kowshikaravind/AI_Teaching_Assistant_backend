from django.db import models

# Create your models here.
from django.db import models

class Student(models.Model):
    # Original Fields
    name = models.CharField(max_length=100)
    roll_number = models.CharField(max_length=20, unique=True)
    class_name = models.CharField(max_length=50)
    parent_number = models.CharField(max_length=15, blank=True, null=True)
    
    # --- NEW FIELDS FOR REGISTRATION FORM ---
    dob = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    nationality = models.CharField(max_length=50, blank=True, null=True)
    blood_group = models.CharField(max_length=10, blank=True, null=True)
    
    parent_name = models.CharField(max_length=100, blank=True, null=True)
    parent_email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    emergency_contact = models.CharField(max_length=15, blank=True, null=True)
    
    # Note: For actual image uploading, you would use models.ImageField() here, 
    # but that requires setting up Django MEDIA_ROOT. We'll skip the backend 
    # image logic for now to keep the API simple, but we will build the UI for it!

    def __str__(self):
        return f"{self.name} ({self.roll_number})"
class TestMark(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    subject = models.CharField(max_length=50 , default="General")
    test_name = models.CharField(max_length=100)
    marks_obtained = models.FloatField()
    total_marks = models.FloatField()
    date_taken = models.DateField()
    def __str__(self):
        return f"{self.student.name} - {self.test_name}"