import os
import base64
import uuid
import traceback
from datetime import datetime
from flask import render_template, request, redirect, url_for, flash, jsonify
from random import choice
import csv
import io
from werkzeug.utils import secure_filename
from app import app, db, DEMO_MODE
from models import Student, Question, ScanResult, Answer, Quiz, Section
from scanner import BubbleSheetScanner

# Initialize scanner
scanner = BubbleSheetScanner()

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'pdf'}

def init_db():
    """Initialize database tables"""
    with app.app_context():
        # Create tables
        db.drop_all()  # Drop existing tables
        db.create_all()  # Recreate tables with new schema

@app.route('/')
def index():
    # Get recent scan results
    recent_scans = ScanResult.query.order_by(ScanResult.scan_date.desc()).limit(10).all()
    # Pass current year to the template for copyright notice
    return render_template('index.html', recent_scans=recent_scans, now=datetime.now())

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('index'))

    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('index'))

    # Get selected template
    template_name = request.form.get('template', 'standard_20')

    try:
        if file and allowed_file(file.filename):
            # Create a unique filename
            original_filename = secure_filename(file.filename)
            extension = original_filename.rsplit('.', 1)[1].lower()
            unique_filename = f"{uuid.uuid4().hex}.{extension}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)

            # Make sure the directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            file.save(filepath)

            # Process the file based on type
            if extension == 'pdf':
                image_paths = scanner.convert_pdf_to_images(filepath)
                if not image_paths:
                    flash('Failed to convert PDF to images', 'danger')
                    return redirect(url_for('index'))

                # Process the first page for now with the selected template
                scanner.current_template = template_name
                result, error = scanner.process_sheet(image_paths[0])
            else:
                # Process image directly with the selected template
                scanner.current_template = template_name
                result, error = scanner.process_sheet(filepath)

            if error:
                flash(f'Error processing file: {error}', 'danger')
                return redirect(url_for('index'))

            # Save results to database
            student_info = result['student']

            # Create or get student
            student = Student.query.filter_by(name=student_info['name']).first()
            if not student:
                student = Student(name=student_info['name'], student_id=student_info.get('id'))
                db.session.add(student)
                db.session.commit()

            # Create scan result
            score_info = result['score']
            scan_result = ScanResult(
                student_id=student.id,
                template_used=result['template'],
                score=score_info['correct'],
                total_questions=score_info['total'],
                percentage=score_info['percentage'],
                image_path=filepath
            )
            db.session.add(scan_result)
            db.session.commit()

            # Add answers
            for q_num, answer in result['answers'].items():
                correct = result['correct_answers'].get(q_num)
                is_correct = answer == correct

                db_answer = Answer(
                    scan_result_id=scan_result.id,
                    question_number=int(q_num),
                    selected_answer=answer,
                    correct_answer=correct,
                    is_correct=is_correct
                )
                db.session.add(db_answer)

            db.session.commit()

            return redirect(url_for('view_result', scan_id=scan_result.id))

        flash('Invalid file type. Please upload PNG, JPG, JPEG, or PDF', 'danger')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Error in upload_file: {str(e)}")
        traceback.print_exc()
        flash(f'An error occurred: {str(e)}', 'danger')
        return redirect(url_for('index'))

@app.route('/result/<int:scan_id>')
def view_result(scan_id):
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 20  # Number of items per page
        
        scan_result = ScanResult.query.get_or_404(scan_id)
        answers = Answer.query.filter_by(scan_result_id=scan_id).order_by(Answer.question_number).paginate(page=page, per_page=per_page, error_out=False)

        return render_template('results.html', scan=scan_result, answers=answers, now=datetime.now())
    except Exception as e:
        flash(f'Error retrieving result: {str(e)}', 'danger')
        return redirect(url_for('index'))

@app.route('/api/results')
def api_results():
    try:
        results = ScanResult.query.order_by(ScanResult.scan_date.desc()).limit(50).all()

        result_list = []
        for result in results:
            result_list.append({
                'id': result.id,
                'student_name': result.student.name,
                'student_id': result.student.student_id,
                'score': result.score,
                'total': result.total_questions,
                'percentage': result.percentage,
                'date': result.scan_date.strftime('%Y-%m-%d %H:%M:%S')
            })

        return jsonify(results=result_list)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/camera')
def camera():
    """Camera capture page"""
    return render_template('camera.html', now=datetime.now())

@app.route('/process_camera_image', methods=['POST'])
def process_camera_image():
    """Process image captured from camera"""
    try:
        data = request.get_json()
        detect_only = data.get('detect_only', False)
        if not data:
            flash('No image data received', 'danger')
            return redirect(url_for('camera'))

        # Get the base64 image data from request JSON
        # This avoids form size limitations
        if request.is_json:
            # Get from JSON payload
            image_data = data.get('image_data')
            template_name = data.get('template', 'standard_20')
        else:
            # Fallback to form data
            image_data = request.form.get('image_data')
            template_name = request.form.get('template', 'standard_20')

        if not image_data:
            flash('No image data received', 'danger')
            return redirect(url_for('camera'))

        # The image data is in format: data:image/png;base64,<actual_base64_data>
        # We need to extract the actual base64 data part
        if ',' in image_data:
            image_data = image_data.split(',')[1]

        # Decode the base64 image
        image_bytes = base64.b64decode(image_data)

        # Save the image to a file
        unique_filename = f"{uuid.uuid4().hex}.png"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)

        # Make sure the directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Write the image to a file
        with open(filepath, 'wb') as f:
            f.write(image_bytes)

        # Process the image with the selected template
        scanner.current_template = template_name
        # Always use process_sheet since it handles both detection and processing
        result, error = scanner.process_sheet(filepath)


        if error:
            if request.is_json:
                return jsonify({'error': error}), 400
            else:
                flash(f'Error processing image: {error}', 'danger')
                return redirect(url_for('camera'))

        # Save results to database (similar to upload_file route)
        student_info = result['student']

        # Create or get student
        student = Student.query.filter_by(name=student_info['name']).first()
        if not student:
            student = Student(name=student_info['name'], student_id=student_info.get('id'))
            db.session.add(student)
            db.session.commit()

        # Create scan result
        score_info = result['score']
        scan_result = ScanResult(
            student_id=student.id,
            template_used=result['template'],
            score=score_info['correct'],
            total_questions=score_info['total'],
            percentage=score_info['percentage'],
            image_path=filepath
        )
        db.session.add(scan_result)
        db.session.commit()

        # Add answers
        for q_num, answer in result['answers'].items():
            correct = result['correct_answers'].get(q_num)
            is_correct = answer == correct

            db_answer = Answer(
                scan_result_id=scan_result.id,
                question_number=int(q_num),
                selected_answer=answer,
                correct_answer=correct,
                is_correct=is_correct
            )
            db.session.add(db_answer)

        db.session.commit()

        # Return appropriate response based on request type
        if request.is_json:
            # Only return success if we have valid student info and answers
            sheet_detected = bool(student_info['name'] and len(result['answers']) > 0)
            return jsonify({
                'success': sheet_detected,
                'scan_id': scan_result.id,
                'message': 'Image processed successfully' if sheet_detected else 'No valid exam sheet detected',
                'result': {
                    'scan_id': scan_result.id,
                    'student': {
                        'name': student.name,
                        'id': student.student_id
                    },
                    'score': {
                        'correct': score_info['correct'],
                        'total': score_info['total'],
                        'percentage': score_info['percentage']
                    }
                }
            })
        else:
            return redirect(url_for('view_result', scan_id=scan_result.id))

    except Exception as e:
        print(f"Error in process_camera_image: {str(e)}")
        traceback.print_exc()

        if request.is_json:
            return jsonify({
                'success': False,
                'error': str(e),
                'message': 'An error occurred while processing the image'
            }), 500
        else:
            flash(f'An error occurred: {str(e)}', 'danger')
            return redirect(url_for('camera'))

@app.route('/download_personalized_template/<int:quiz_id>/<int:student_id>')
def download_personalized_template(quiz_id, student_id):
    """Generate and download a personalized template for a student"""
    try:
        quiz = Quiz.query.get_or_404(quiz_id)
        student = Student.query.get_or_404(student_id)
        
        # Create PDF with student info
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        import io
        
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        # Draw header with logo (if exists)
        try:
            c.drawImage('static/img/logo.jpg', 0.5*inch, height - 1.2*inch, width=1*inch, height=1*inch)
        except:
            pass
            
        # Draw title and institution
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(width/2, height - 0.75*inch, quiz.title)
        c.setFont("Helvetica", 12)
        c.drawCentredString(width/2, height - 1*inch, "Answer Sheet")
        
        # Draw student info section
        c.setFont("Helvetica-Bold", 12)
        c.rect(1.5*inch, height - 2.2*inch, width - 3*inch, 0.8*inch, stroke=1, fill=0)
        c.setFont("Helvetica", 11)
        c.drawString(1.7*inch, height - 1.7*inch, f"Name: {student.name}")
        c.drawString(1.7*inch, height - 2*inch, f"Student ID: {student.student_id}")
        c.drawString(width - 3.5*inch, height - 1.7*inch, f"Section: {student.section.name}")
        c.drawString(width - 3.5*inch, height - 2*inch, f"Date: _________________")
        
        # Draw instructions
        c.setFont("Helvetica-Bold", 10)
        c.drawString(1*inch, height - 2.6*inch, "Instructions:")
        c.setFont("Helvetica", 10)
        c.drawString(1*inch, height - 2.9*inch, "1. Use a pencil to completely fill in the bubble of your chosen answer.")
        c.drawString(1*inch, height - 3.2*inch, "2. Erase completely any answer you wish to change.")
        c.drawString(1*inch, height - 3.5*inch, "3. Make your marks heavy and dark.")
        
        # Calculate columns based on number of items
        items_per_column = 25
        num_columns = (quiz.num_items + items_per_column - 1) // items_per_column
        column_width = (width - 2*inch) / num_columns
        
        # Draw answer bubbles
        start_y = height - 4.2*inch
        row_height = 0.25*inch
        
        for col in range(num_columns):
            col_x = 1*inch + col * column_width
            
            # Draw column header
            c.setFillColor(colors.grey)
            c.rect(col_x, start_y + 0.1*inch, column_width - 0.1*inch, 0.3*inch, stroke=1, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
            start_num = col * items_per_column + 1
            end_num = min((col + 1) * items_per_column, quiz.num_items)
            c.drawCentredString(col_x + column_width/2, start_y + 0.2*inch, f"Questions {start_num}-{end_num}")
            
            c.setFillColor(colors.black)
            for i in range(start_num, end_num + 1):
                y = start_y - ((i - start_num) * row_height)
                
                # Question number
                c.setFont("Helvetica", 8)
                c.drawString(col_x + 0.1*inch, y, f"{i}.")
                
                # Answer bubbles
                bubble_x = col_x + 0.4*inch
                for j, letter in enumerate(['A', 'B', 'C', 'D']):
                    c.circle(bubble_x + j*0.3*inch, y + 0.07*inch, 5, stroke=1, fill=0)
                    c.drawString(bubble_x + j*0.3*inch - 2, y - 0.02*inch, letter)
        
        c.save()
        
        # Get PDF from buffer
        pdf = buffer.getvalue()
        buffer.close()
        
        # Create response
        response = app.response_class(
            pdf,
            mimetype='application/pdf',
            direct_passthrough=True
        )
        
        filename = f"{quiz.title}_{student.name}_{student.student_id}.pdf"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        return response
        
    except Exception as e:
        flash(f'Error generating template: {str(e)}', 'danger')
        return redirect(url_for('student_list'))

@app.route('/download_template/<template_type>')
def download_template(template_type):
    """Download a template based on the selected format"""
    try:
        template_file = None
        if template_type == 'standard_20':
            template_file = 'standard_20.pdf'
            template_name = 'Standard 20 Questions Template'
        elif template_type == 'extended_50':
            template_file = 'extended_50.pdf'
            template_name = 'Extended 50 Questions Template'
        elif template_type == 'comprehensive_100':
            template_file = 'comprehensive_100.pdf'
            template_name = 'Comprehensive 100 Questions Template'
        else:
            flash('Invalid template type requested', 'danger')
            return redirect(url_for('index'))

        # Get the full path to the template file
        template_path = os.path.join(app.static_folder, 'templates', template_file)

        # Make sure the file exists
        if not os.path.exists(template_path):
            flash(f'Template file not found: {template_file}', 'danger')
            return redirect(url_for('index'))

        # Read the file in binary mode
        with open(template_path, 'rb') as f:
            binary_pdf = f.read()

        # Create a response with the correct content type
        response = app.response_class(
            binary_pdf,
            mimetype='application/pdf',
            direct_passthrough=True
        )

        # Set Content-Disposition header to attachment to force download
        filename = f"MattChecker_{template_file}"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        response.headers['Content-Type'] = 'application/pdf'

        return response
    except Exception as e:
        flash(f'Error downloading template: {str(e)}', 'danger')
        print(f"Template download error: {str(e)}")
        traceback.print_exc()
        return redirect(url_for('index'))

@app.route('/setup')
@app.route('/batch-scan', methods=['GET', 'POST'])
def batch_scan():
    """Handle batch scanning of multiple answer sheets with error tracking"""
    if request.method == 'POST':
        if 'files[]' not in request.files:
            flash('No files selected', 'danger')
            return redirect(url_for('batch_scan'))

        files = request.files.getlist('files[]')
        template_name = request.form.get('template', 'standard_20')
        results = []
        errors = []

        for file in files:
            if file and allowed_file(file.filename):
                # Create unique filename
                original_filename = secure_filename(file.filename)
                extension = original_filename.rsplit('.', 1)[1].lower()
                unique_filename = f"{uuid.uuid4().hex}.{extension}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)

                file.save(filepath)

                try:
                    # Process file based on type
                    if extension == 'pdf':
                        image_paths = scanner.convert_pdf_to_images(filepath)
                        if not image_paths:
                            continue
                        scanner.current_template = template_name
                        result, error = scanner.process_sheet(image_paths[0])
                    else:
                        scanner.current_template = template_name
                        result, error = scanner.process_sheet(filepath)

                    if error:
                        errors.append({
                            'filename': original_filename,
                            'error': error
                        })
                        continue

                    # Save results to database
                    student_info = result['student']
                    student = Student.query.filter_by(name=student_info['name']).first()
                    if not student:
                        student = Student(name=student_info['name'], student_id=student_info.get('id'))
                        db.session.add(student)
                        db.session.commit()

                    score_info = result['score']
                    scan_result = ScanResult(
                        student_id=student.id,
                        template_used=result['template'],
                        score=score_info['correct'],
                        total_questions=score_info['total'],
                        percentage=score_info['percentage'],
                        image_path=filepath
                    )
                    db.session.add(scan_result)
                    db.session.commit()

                    for q_num, answer in result['answers'].items():
                        correct = result['correct_answers'].get(q_num)
                        is_correct = answer == correct
                        db_answer = Answer(
                            scan_result_id=scan_result.id,
                            question_number=int(q_num),
                            selected_answer=answer,
                            correct_answer=correct,
                            is_correct=is_correct
                        )
                        db.session.add(db_answer)

                    db.session.commit()
                    results.append(scan_result.id)

                except Exception as e:
                    print(f"Error processing file {original_filename}: {str(e)}")
                    traceback.print_exc()

        total_files = len(files)
        success_count = len(results)
        error_count = len(errors)

        if success_count > 0:
            flash(f'Successfully processed {success_count} out of {total_files} files', 'success')
            if error_count > 0:
                for error in errors:
                    flash(f'Error in {error["filename"]}: {error["error"]}', 'warning')
            return redirect(url_for('index'))
        else:
            flash('No files were successfully processed', 'danger')
            for error in errors:
                flash(f'Error in {error["filename"]}: {error["error"]}', 'danger')
            return redirect(url_for('batch_scan'))

    return render_template('batch_scan.html', now=datetime.now())

@app.route('/setup')
def setup_db():
    """Setup route for initializing test data with student names from student_classes"""
    try:
        # Clear existing data
        Answer.query.delete()
        ScanResult.query.delete()
        Student.query.delete()
        Question.query.delete()
        db.session.commit()

        # Insert questions with correct answers (A, B, C, D in rotation)
        for i in range(1, 101):
            answer = chr(ord('A') + ((i-1) % 4))  # A, B, C, D in rotation
            question = Question(question_id=i, correct_answer=answer)
            db.session.add(question)

        # Insert students from the checker_db user_info table
        students_data = [
            ('ADALID, JOHN SIMON DIAZ', '20220003'),
            ('LEYNES, CHRISTOPHER FIESTA', '20220007'),
            ('DOMINGO, GABRIEL CUBALAN', '20220012'),
            ('VERGARA, JOVIN LEE', '20220026'),
            ('CASTRO, KURT LOUIE CASTRO', '20220027'),
            ('DAVAC, VINCENT AHRON MANTUHAC', '20220041'),
            ('CHAN, IAN MYRON LUNA', '20220060'),
            ('CRUZ, DANIKEN SANTOS', '20220078'),
            ('COLENDRES, SHERWIN BONIFACIO', '20220081'),
            ('BIAGTAS, ALTHEA NICOLE LAGUNA', '20220085'),
            ('VINLUAN, AILA RAMOS', '20220086'),
            ('BAENA, VINCE IVERSON CAMACHO', '20220111'),
            ('PUNO, LOURINE ASHANTI MATEL', '20220112'),
            ('URETA, JAN EDMAINE DELA TORRE', '20220129'),
            ('MANGALINDAN, JEROME TAMAYO', '20220152'),
            ('COMPETENTE, ANNENA CAMBI', '19110182'),
            ('DELOS REYES, AARON VINCENT MANLAPAZ', '20190994'),
            ('CUADERNO, NICOLE MAYA', '20201202'),
            ('BARCENA, DIVINE GRACE DAWAL', '20210004'),
            ('ANZA, AIRA JOYCE NAVARRO', '20211490')
        ]

        for name, student_id in students_data:
            student = Student(name=name, student_id=student_id)
            db.session.add(student)

        db.session.commit()

        flash('Database initialized with student data from checker_db', 'success')
        return redirect(url_for('index'))
    except Exception as e:
        flash(f'Error setting up database: {str(e)}', 'danger')
        print(f"Database setup error: {str(e)}")
        traceback.print_exc()
        return redirect(url_for('index'))
@app.route('/download_result/<int:scan_id>')
def download_result(scan_id):
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from io import BytesIO
        from reportlab.lib.units import inch

        scan_result = ScanResult.query.get_or_404(scan_id)
        answers = Answer.query.filter_by(scan_result_id=scan_id).order_by(Answer.question_number).all()

        # Create PDF in memory
        buffer = BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)

        # Header
        p.setFont("Helvetica-Bold", 16)
        p.drawString(1*inch, 10*inch, "Scan Results")
        p.setFont("Helvetica", 12)
        p.drawString(1*inch, 9.5*inch, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Student Info
        p.setFont("Helvetica-Bold", 12)
        p.drawString(1*inch, 8.5*inch, "Student Information")
        p.setFont("Helvetica", 12)
        p.drawString(1*inch, 8*inch, f"Name: {scan_result.student.name}")
        p.drawString(1*inch, 7.7*inch, f"Student ID: {scan_result.student.student_id}")
        p.drawString(1*inch, 7.4*inch, f"Score: {scan_result.score}/{scan_result.total_questions} ({scan_result.percentage}%)")
        p.drawString(1*inch, 7.1*inch, f"Scan Date: {scan_result.scan_date.strftime('%Y-%m-%d %H:%M:%S')}")

        # Answers Table
        y = 6*inch
        p.setFont("Helvetica-Bold", 12)
        p.drawString(1*inch, y, "Question")
        p.drawString(2*inch, y, "Selected")
        p.drawString(3*inch, y, "Correct")
        p.drawString(4*inch, y, "Status")

        p.setFont("Helvetica", 10)
        for answer in answers:
            y -= 20
            if y < inch:  # Start new page if needed
                p.showPage()
                y = 10*inch
                p.setFont("Helvetica", 10)

            p.drawString(1*inch, y, str(answer.question_number))
            p.drawString(2*inch, y, answer.selected_answer or '—')
            p.drawString(3*inch, y, answer.correct_answer)
            p.setFillColor(colors.green if answer.is_correct else colors.red)
            p.drawString(4*inch, y, 'Correct' if answer.is_correct else 'Incorrect')
            p.setFillColor(colors.black)

        p.save()

        # Get the value from the BytesIO buffer
        pdf = buffer.getvalue()
        buffer.close()

        # Create response
        response = app.response_class(
            pdf,
            mimetype='application/pdf',
            direct_passthrough=True
        )

        # Set the download filename
        filename = f"scan_result_{scan_id}_{datetime.now().strftime('%Y%m%d')}.pdf"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

        return response
    except Exception as e:
        flash(f'Error generating PDF: {str(e)}', 'danger')
        return redirect(url_for('view_result', scan_id=scan_id))
@app.route('/quiz/create', methods=['GET', 'POST'])
def create_quiz():
    if request.method == 'POST':
        title = request.form.get('title')
        num_items = int(request.form.get('num_items'))
        answer_key = request.form.get('answer_key')
        section_id = request.form.get('section_id')
        
        if not all([title, num_items, answer_key, section_id]):
            flash('All fields are required', 'danger')
            return redirect(url_for('create_quiz'))
            
        # Validate answer key length matches num_items
        if len(answer_key) != num_items:
            flash('Answer key length must match number of items', 'danger')
            return redirect(url_for('create_quiz'))
            
        quiz = Quiz(title=title, num_items=num_items, answer_key=answer_key, section_id=section_id)
        db.session.add(quiz)
        db.session.commit()
        
        flash('Quiz created successfully', 'success')
        return redirect(url_for('list_quizzes'))

    sections = Section.query.order_by(Section.name).all()
    return render_template('create_quiz.html', sections=sections, now=datetime.now())

@app.route('/quiz/list')
def list_quizzes():
    quizzes = Quiz.query.order_by(Quiz.created_at.desc()).all()
    return render_template('list_quizzes.html', quizzes=quizzes, now=datetime.now())

@app.route('/quiz/generate_key', methods=['POST'])
def generate_key():
    num_items = int(request.form.get('num_items', 20))
    choices = ['A', 'B', 'C', 'D']
    answer_key = ''.join(choice(choices) for _ in range(num_items))
    return jsonify({'answer_key': answer_key})
@app.route('/students', methods=['GET', 'POST'])
def student_list():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected', 'danger')
            return redirect(url_for('student_list'))
        
        file = request.files['file']
        section_id = request.form.get('section_id')
        
        if file.filename == '':
            flash('No file selected', 'danger')
            return redirect(url_for('student_list'))
            
        if not file.filename.endswith('.csv'):
            flash('Please upload a CSV file', 'danger')
            return redirect(url_for('student_list'))
            
        try:
            # Clear existing students from the target section
            Student.query.filter_by(section_id=section_id).delete()
            
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_reader = csv.reader(stream)
            next(csv_reader)  # Skip header row
            
            for row in csv_reader:
                if len(row) >= 2:  # Make sure we have both name and student ID
                    name = row[0].strip()
                    student_id = row[1].strip()
                    student = Student(name=name, student_id=student_id, section_id=section_id)
                    db.session.add(student)
            
            db.session.commit()
            flash('Students imported successfully', 'success')
        except Exception as e:
            flash(f'Error importing students: {str(e)}', 'danger')
            
        return redirect(url_for('student_list'))
        
    students = Student.query.order_by(Student.name).all()
    sections = Section.query.order_by(Section.name).all()
    return render_template('students.html', students=students, sections=sections, now=datetime.now())

@app.route('/section/create', methods=['POST'])
def create_section():
    name = request.form.get('name')
    if name:
        section = Section(name=name)
        db.session.add(section)
        db.session.commit()
        flash('Section created successfully', 'success')
    return redirect(url_for('student_list'))

@app.route('/students/delete/<int:student_id>', methods=['POST'])
def delete_student(student_id):
    student = Student.query.get_or_404(student_id)
    db.session.delete(student)
    db.session.commit()
    flash('Student deleted successfully', 'success')
    return redirect(url_for('student_list'))

@app.route('/section/delete/<int:section_id>', methods=['POST'])
def delete_section(section_id):
    section = Section.query.get_or_404(section_id)
    # Delete all quizzes associated with the section first
    Quiz.query.filter_by(section_id=section_id).delete()
    # Delete all students in the section
    Student.query.filter_by(section_id=section_id).delete()
    # Delete the section
    db.session.delete(section)
    db.session.commit()
    flash('Section and associated data deleted successfully', 'success')
    return redirect(url_for('student_list'))


