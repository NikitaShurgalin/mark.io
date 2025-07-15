def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    if b == 0:
        return 'Ошибка: деление на ноль!'
    return a / b

if __name__ == "__main__":
    print("Простой калькулятор")
    print("Доступные операции: +, -, *, /")
    a = float(input("Введите первое число: "))
    op = input("Введите операцию (+, -, *, /): ")
    b = float(input("Введите второе число: "))

    if op == '+':
        result = add(a, b)
    elif op == '-':
        result = subtract(a, b)
    elif op == '*':
        result = multiply(a, b)
    elif op == '/':
        result = divide(a, b)
    else:
        result = 'Ошибка: неизвестная операция!'

    print(f"Результат: {result}")