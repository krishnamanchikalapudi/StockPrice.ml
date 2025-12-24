import time
import unittest
from src.common.PostgreSqllDbConnection import PostgreSqllDbConnection

class PostgreSqllDbConnectionTests(unittest.TestCase):
    def setUp(self):
        self.db = PostgreSqllDbConnection("test_db")
        self.db.connect()

    def tearDown(self):
        self.db.close()

    def test_connection(self):
        self.assertTrue(self.db.engine is not None) 

    def test_select(self):
        results = self.db.select("SELECT * FROM dual")
        self.assertTrue(len(results) > 0)
        