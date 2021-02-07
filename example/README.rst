Example Application
===================

Use Python 3.8 to set up and run with these commands:

.. code-block:: sh

   python -m venv venv
   source venv/bin/activate
   python -m pip install -U pip
   python -m pip install -r requirements.txt -e ..
   DEBUG=1 python manage.py runserver

Open it at http://127.0.0.1:8000/ .

Browse the individual examples.
Take them apart in your browser’s network tab and read the commented source code to see what’s going on!
