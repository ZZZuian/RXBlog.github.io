from flask_wtf import FlaskForm as Form
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired


class RenameChronofileForm(Form):
    new_name = StringField('输入时间记录本的新名称：', \
                           validators=[DataRequired()])
    submit = SubmitField('重命名时间记录本')


class RenameAuthorForm(Form):
    new_name = StringField('输入新的个人昵称：', \
                           validators=[DataRequired()])
    submit = SubmitField('更新昵称')
