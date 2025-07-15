import re

def calculate_expression(expr):
    match = re.fullmatch(r'\s*(-?\d+(?:\.\d*)?)\s*([+\-*/])\s*(-?\d+(?:\.\d*)?)\s*', expr)
    if not match:
        return 'Ошибка: неверный формат выражения!'
    a, op, b = match.groups()
    a = float(a)
    b = float(b)
    if op == '+':
        return a + b
    elif op == '-':
        return a - b
    elif op == '*':
        return a * b
    elif op == '/':
        if b == 0:
            return 'Ошибка: деление на ноль!'
        return a / b
    else:
        return 'Ошибка: неизвестная операция!'

if __name__ == "__main__":
    print("Простой калькулятор (пример: 5+5, 10 / 2)")
    expr = input("Введите выражение: ")
    result = calculate_expression(expr)
    print(f"Результат: {result}")