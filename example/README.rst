Example Application
===================

Use Python 3.8 to run, with vanilla venv, pip, and Django:

.. code-block:: sh

   python -m venv venv
   source venv/bin/activate
   python -m pip install -U pip -r requirements.txt -e ..
   python manage.py migrate
   python manage.py generatedata
   DEBUG=1 python manage.py runserver

Open it at http://127.0.0.1:8000/ .
From there you can test some htmx features which use django-htmx.
