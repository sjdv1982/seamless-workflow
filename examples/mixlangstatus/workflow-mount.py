from seamless.workflow import Context, Cell, Transformer
ctx = Context()

def random_text(length:int, seed:int):
    import time
    import random
    time.sleep(5)
    random.seed(seed)
    alphabet = [chr(n) for n in range(ord('A'), ord('Z')+1)]
    letters = random.choices(alphabet, k=length)
    return "".join(letters)

ctx.random_text = random_text
ctx.compute()
ctx.random_text_code = Cell("python")
ctx.random_text_code.set_buffer(ctx.random_text.code)
ctx.random_text_code.mount("random_text.py", "rw", "cell")
ctx.random_text.code = ctx.random_text_code
ctx.length = Cell("int")
ctx.length.mount("length.txt", "rw", "cell")
ctx.seed = Cell("int")
ctx.seed.mount("seed.txt", "rw", "cell")
ctx.random_text.pins.length = ctx.length
ctx.random_text.pins.seed = ctx.seed

ctx.text1 = Cell("text")
ctx.text1 = ctx.random_text.result
ctx.text1.mount("text1.txt", "w")

bash_code = """
sleep 5
sed 's/C/Y/g' text1 > RESULT
"""
ctx.modify_text_code = Cell("text") 
ctx.modify_text_code.set(bash_code)
ctx.modify_text_code.mount("modify_text.sh", "rw", "cell")

ctx.modify_text = Transformer("bash")
ctx.modify_text.code = ctx.modify_text_code
ctx.modify_text.pins.text1 = ctx.text1

ctx.text2 = Cell("text")
ctx.text2 = ctx.modify_text.result
ctx.text2.mount("text2.txt", "w")
