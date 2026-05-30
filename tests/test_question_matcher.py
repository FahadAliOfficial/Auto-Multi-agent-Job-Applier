import unittest
import aiosqlite
from src.handlers.question_matcher import QuestionMatcher, CONFIDENCE_THRESHOLD

class TestQuestionMatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create an in-memory database for testing
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        
        # Create the screening_answers table
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS screening_answers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER,
                question        TEXT NOT NULL,
                answer          TEXT NOT NULL,
                confidence      REAL DEFAULT 1.0,
                was_manual      BOOLEAN DEFAULT 0,
                times_used      INTEGER DEFAULT 1,
                answered_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await self.db.commit()
        
        self.matcher = QuestionMatcher(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_find_answer_exact_match(self):
        # Insert a question-answer pair
        await self.db.execute(
            "INSERT INTO screening_answers (question, answer) VALUES (?, ?)",
            ("Do you have experience with Python?", "Yes, 5 years")
        )
        await self.db.commit()
        
        # Test exact match
        answer, confidence = await self.matcher.find_answer("Do you have experience with Python?")
        self.assertEqual(answer, "Yes, 5 years")
        self.assertEqual(confidence, 1.0)

    async def test_find_answer_fuzzy_match(self):
        # Insert a question-answer pair
        await self.db.execute(
            "INSERT INTO screening_answers (question, answer) VALUES (?, ?)",
            ("How many years of work experience do you have?", "5")
        )
        await self.db.commit()
        
        # Test fuzzy match with minor differences in capitalization/punctuation
        answer, confidence = await self.matcher.find_answer("how many years of work experience do you have??")
        self.assertEqual(answer, "5")
        self.assertTrue(confidence >= CONFIDENCE_THRESHOLD)

    async def test_find_answer_no_match(self):
        # Insert a question-answer pair
        await self.db.execute(
            "INSERT INTO screening_answers (question, answer) VALUES (?, ?)",
            ("How many years of work experience do you have?", "5")
        )
        await self.db.commit()
        
        # Test a completely different question
        answer, confidence = await self.matcher.find_answer("Do you require visa sponsorship?")
        self.assertIsNone(answer)
        self.assertTrue(confidence < CONFIDENCE_THRESHOLD)

    async def test_save_answer_new(self):
        # Save a new answer
        await self.matcher.save_answer("What is your expected salary?", "Negotiable", job_id=123)
        
        # Verify it was inserted
        cursor = await self.db.execute("SELECT * FROM screening_answers WHERE question = ?", ("What is your expected salary?",))
        row = await cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(row["answer"], "Negotiable")
        self.assertEqual(row["job_id"], 123)
        self.assertEqual(row["times_used"], 1)
        self.assertEqual(row["was_manual"], 1)

    async def test_save_answer_increment(self):
        # Insert a question-answer pair first
        await self.db.execute(
            "INSERT INTO screening_answers (question, answer, times_used) VALUES (?, ?, ?)",
            ("Are you authorized to work in the US?", "Yes", 2)
        )
        await self.db.commit()
        
        # Save the same answer again
        await self.matcher.save_answer("Are you authorized to work in the US?", "Yes")
        
        # Verify times_used was incremented
        cursor = await self.db.execute("SELECT times_used FROM screening_answers WHERE question = ?", ("Are you authorized to work in the US?",))
        row = await cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(row["times_used"], 3)
