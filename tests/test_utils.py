"""Tests for mypackage.utils module.

This module contains unit tests for the utility functions in mypackage.utils.
"""

import pytest
from mypackage.utils import greet_user, calculate_average, is_positive


class TestGreetUser:
    """Tests for the greet_user function."""

    def test_greet_user_default_greeting(self) -> None:
        """Test greet_user with default greeting."""
        result = greet_user("Alice")
        assert result == "Hello, Alice!"

    def test_greet_user_custom_greeting(self) -> None:
        """Test greet_user with custom greeting."""
        result = greet_user("Bob", "Hi")
        assert result == "Hi, Bob!"

    def test_greet_user_empty_name_raises_error(self) -> None:
        """Test greet_user raises ValueError for empty name."""
        with pytest.raises(ValueError, match="Name cannot be empty"):
            greet_user("")

    def test_greet_user_various_greetings(self) -> None:
        """Test greet_user with various greeting words."""
        assert greet_user("World", "Welcome") == "Welcome, World!"
        assert greet_user("Friend", "Hey") == "Hey, Friend!"
        assert greet_user("Developer", "Greetings") == "Greetings, Developer!"


class TestCalculateAverage:
    """Tests for the calculate_average function."""

    def test_calculate_average_basic(self) -> None:
        """Test calculate_average with a basic list."""
        result = calculate_average([1, 2, 3, 4, 5])
        assert result == 3.0

    def test_calculate_average_empty_list(self) -> None:
        """Test calculate_average with empty list returns None."""
        result = calculate_average([])
        assert result is None

    def test_calculate_average_single_element(self) -> None:
        """Test calculate_average with single element."""
        result = calculate_average([42.0])
        assert result == 42.0

    def test_calculate_average_floats(self) -> None:
        """Test calculate_average with float values."""
        result = calculate_average([1.5, 2.5, 3.5])
        assert result == 2.5

    def test_calculate_average_negative_numbers(self) -> None:
        """Test calculate_average with negative numbers."""
        result = calculate_average([-1, 0, 1])
        assert result == 0.0

    def test_calculate_average_mixed_numbers(self) -> None:
        """Test calculate_average with mixed positive and negative numbers."""
        result = calculate_average([-5, 5])
        assert result == 0.0


class TestIsPositive:
    """Tests for the is_positive function."""

    def test_is_positive_with_positive_number(self) -> None:
        """Test is_positive returns True for positive numbers."""
        assert is_positive(5) is True
        assert is_positive(0.1) is True
        assert is_positive(100) is True

    def test_is_positive_with_negative_number(self) -> None:
        """Test is_positive returns False for negative numbers."""
        assert is_positive(-5) is False
        assert is_positive(-0.1) is False
        assert is_positive(-100) is False

    def test_is_positive_with_zero(self) -> None:
        """Test is_positive returns False for zero."""
        assert is_positive(0) is False

    def test_is_positive_with_float(self) -> None:
        """Test is_positive works with float values."""
        assert is_positive(3.14) is True
        assert is_positive(-2.71) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
