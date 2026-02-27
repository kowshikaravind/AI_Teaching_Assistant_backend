from rest_framework import generics
from .models import Student, TestMark
from .serializers import StudentSerializer, TestMarkSerializer

class StudentListCreateView(generics.ListCreateAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

class StudentRetriveDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

class TestMarkListCreateView(generics.ListCreateAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer
    
    def get_queryset(self):
        student_id = self.request.query_params.get('student_id')
        if student_id:
            return TestMark.objects.filter(student_id=student_id)
        return TestMark.objects.all()

class TestMarkRetrieveDestroyView(generics.RetrieveDestroyAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer