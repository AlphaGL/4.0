# movies/forms.py
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.forms import inlineformset_factory
from .models import Movie, Comment, Review, DownloadLink, Category

from django import forms

class MovieForm(forms.ModelForm):
    class Meta:
        model = Movie
        fields = [
            'title', 'title_b', 'title_b_updated_at', 'completed',
            'description', 'video_url', 'download_url', 'image_url',
            'categories', 'scraped'
        ]
        widgets = {
            'title': forms.TextInput(attrs={
                'style': 'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                         'border-radius: 8px; padding: 10px; font-size: 16px; font-weight: 600; '
                         'font-family: Arial, sans-serif; margin-bottom: 12px;',
                'placeholder': 'Movie title',
            }),
            'title_b': forms.TextInput(attrs={
                'style': 'background-color: #1c1c1e; color: #ccc; border: 1px solid #444; '
                         'border-radius: 8px; padding: 8px; margin-bottom: 12px;',
                'placeholder': 'New episode title (optional)',
            }),
            'title_b_updated_at': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
                'style': 'background-color: #1c1c1e; color: #ccc; border: 1px solid #444; '
                         'border-radius: 8px; padding: 8px; margin-bottom: 12px;',
            }),
            'completed': forms.CheckboxInput(attrs={
                'style': 'margin-left: 10px;',
            }),
            'scraped': forms.CheckboxInput(attrs={
                'style': 'margin-left: 10px;',
            }),
            'description': forms.Textarea(attrs={
                'rows': 3,
                'style': 'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                         'border-radius: 8px; padding: 10px; font-size: 14px; font-family: Arial, sans-serif; '
                         'resize: vertical;',
                'placeholder': 'Enter description...',
            }),
            'video_url': forms.URLInput(attrs={
                'style': 'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                         'border-radius: 8px; padding: 10px; font-size: 14px; font-family: Arial, sans-serif; '
                         'margin-bottom: 12px;',
                'placeholder': 'https://...',
            }),
            'download_url': forms.URLInput(attrs={
                'style': 'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                         'border-radius: 8px; padding: 10px; font-size: 14px; font-family: Arial, sans-serif; '
                         'margin-bottom: 12px;',
                'placeholder': 'https://...',
            }),
            'image_url': forms.URLInput(attrs={
                'style': 'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                         'border-radius: 8px; padding: 10px; font-size: 14px; font-family: Arial, sans-serif; '
                         'margin-bottom: 12px;',
                'placeholder': 'https://...',
            }),
            'categories': forms.CheckboxSelectMultiple(attrs={
                'style': 'color: #f0f0f0; margin-top: 8px;',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['categories'].queryset = Category.objects.all()

class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['content']
        widgets = {
            'content': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': 'Add a comment...',
                'style': (
                    'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                    'border-radius: 8px; padding: 12px; font-size: 14px; font-family: Arial, sans-serif; '
                    'resize: none; width: 100%;'
                ),
            }),
        }

class ReviewForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The rating input is hidden (the visible stars drive it via JS) and a
        # browser can't show its native "required" validation prompt on a
        # hidden field — it just silently blocks the submit instead, so the
        # JS-side "pick a star rating" message never gets a chance to show.
        # add_review() already validates 1-10 server-side, so this is safe.
        self.fields['rating'].required = False

    class Meta:
        model = Review
        fields = ['rating', 'content']
        widgets = {
            'rating': forms.NumberInput(attrs={
                'min': 1, 'max': 10, 'step': 1,
                'id': 'id_review_rating',
                'style': 'display:none;',
            }),
            'content': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'What did you think? (optional)',
                'style': (
                    'background-color: #1c1c1e; color: #f0f0f0; border: 1px solid #444; '
                    'border-radius: 8px; padding: 12px; font-size: 14px; font-family: Arial, sans-serif; '
                    'resize: none; width: 100%;'
                ),
            }),
        }


class DownloadLinkForm(forms.ModelForm):
    class Meta:
        model = DownloadLink
        fields = ['label', 'url']
        widgets = {
            'label': forms.TextInput(attrs={'placeholder': 'e.g. Episode 1 (720p)', 'class': 'form-control'}),
            'url': forms.URLInput(attrs={'placeholder': 'https://...', 'class': 'form-control'}),
        }

DownloadLinkFormSet = inlineformset_factory(
    Movie, DownloadLink, form=DownloadLinkForm,
    extra=100, can_delete=True
)
