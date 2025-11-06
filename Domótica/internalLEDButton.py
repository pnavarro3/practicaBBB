from flask import Flask, render_template, request

LEDPATH='/sys/class/leds/beaglebone:green:usr3/brightness'

app = Flask(__name__)

@app.route("/",methods=['GET','POST'])
def internalLEDButton():
    if request.method == 'POST':
        if "putON" in request.form:
            f=open(LEDPATH,"w")
            f.seek(0)
            f.write("1")
            f.close()
            #pass # do something
        elif  "putOFF" in request.form:
            f=open(LEDPATH,"w")
            f.seek(0)
            f.write("0")
            f.close()
            #pass # do something else
        else:
            pass # unknown
    elif request.method == 'GET':
        return render_template('index.html')
    
    return render_template("index.html")
