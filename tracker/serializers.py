from rest_framework import serializers
from .models import Student, TestMark

class TestMarkSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestMark
        fields = ['id', 'student', 'subject', 'test_name', 'marks_obtained', 'total_marks', 'date_taken']

class StudentSerializer(serializers.ModelSerializer):
    test_marks = TestMarkSerializer(source='testmark_set', many=True, read_only=True)
    
    class Meta:
        model = Student
        fields = [
            'id', 
            'roll_number', 
            'name', 
            'class_name', 
            'dob', 
            'gender', 
            'nationality', 
            'blood_group', 
            'parent_name', 
            'parent_number', 
            'parent_email', 
            'address', 
            'emergency_contact', 
            'test_marks'
        ]