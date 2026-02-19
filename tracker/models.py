from django.db import models

# Create your models here.
class Student(models.Model):
    roll_number = models.CharField(max_length = 20, unique = True)
    name = models.CharField(max_length=100)
    parent_number = models.CharField(max_length=15,default="")
    class_name = models.CharField(max_length=5)
    def __str__(self):
        return self.name
