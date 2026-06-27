from app import app
app.config.from_object('config')

# Run app
if __name__ == '__main__':
    app.run(debug=app.config['DEBUG'])