from django import forms


class OddNumberForm(forms.Form):
    number = forms.IntegerField()
