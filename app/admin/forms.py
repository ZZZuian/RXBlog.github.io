from flask_wtf import FlaskForm as Form
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired


class RenameChronofileForm(Form):
    new_name = StringField('Enter new name for chronofile:', \
                           validators=[DataRequired()])
    submit = SubmitField('Rename chronofile')


class RenameAuthorForm(Form):
    new_name = StringField('Enter new author name:', \
                           validators=[DataRequired()])
    submit = SubmitField('Rename author')