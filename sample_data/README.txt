READYSTAMP SAMPLE DATA
=====================

Everything in this folder is made up. The law firms, companies, people,
invoices and emails are fictional, so it is safe to stamp, export, share
or delete. It exists only so you can try ReadyStamp without real client
documents.

The story behind the documents: a fictional logistics company, Blue Heron
Logistics Co., is in a small billing dispute with Acme Widget Holdings.
The set holds the kind of papers such a case produces: letters, contracts,
emails, an invoice, handwritten notes, a phone photo of a receipt and a
landscape log sheet.


THE THREE FOLDERS
-----------------

1_loose_files
    12 documents as plain image files, 18 pages in total. Use these to
    test the "drop files" way of loading.

      01_engagement_letter.tif           1 page, black & white scan (TIFF)
      02_services_agreement.tif          4 pages in one TIFF file (multi-page)
      03_email_late_shipments.png        1 page, printed email (PNG)
      04_invoice.jpg                     1 page, colour invoice (JPG)
      05_handwritten_call_notes.jpg      1 page, colour notepad (JPG)
      06_mutual_nda.tif                  3 pages in one TIFF file (multi-page)
      07_demand_letter.tif               1 page, black & white scan (TIFF)
      08_delivery_receipt_photo.jpg      1 page, phone photo of a receipt (JPG)
      09_lease_amendment.png             1 page, contract (PNG)
      10_settlement_term_sheet.tif       2 pages in one TIFF file (multi-page)
      11_email_production_instructions.png  1 page, printed email (PNG)
      12_dock_log_landscape.tif          1 page, sideways (landscape) page

2_production_with_load_file
    The same 12 documents arranged the way a real e-discovery production
    arrives: one image per page, numbered FAKE0000001 to FAKE0000018,
    plus two "load files" that describe them.

      VOL001/DATA/VOL001.opt   The OPT load file. It lists every page
                               image and says where each document starts.
      VOL001/DATA/VOL001.dat   The DAT load file. One row of details per
                               document: Bates range, page count,
                               custodian, date, document type, title and
                               a confidentiality value.
      VOL001/IMAGES/0001/      The 18 page images (TIFF, JPG and PNG).

    This set is clean: it should load with no problems.

3_production_with_problems
    A small, deliberately broken production (3 documents) to show how
    ReadyStamp's load report warns you about bad data. It contains:

      - an error:   page BAD0000004 is listed in the OPT, but its image
                    file is missing
      - a warning:  document BAD0000002 has no row in the DAT
      - a warning:  the DAT has a row (BAD0000099) for a document that
                    does not exist
      - a note:     an extra image (stray_extra_page.jpg) sits in the
                    images folder but nothing refers to it

    ReadyStamp will refuse to run until the document with the error is
    removed, which is the expected behaviour.


HOW TO LOAD THEM IN THE READYSTAMP SCREEN
-----------------------------------------

First unzip the download. This folder is then at:
    readystamp-main/sample_data
(for example in your Downloads folder:
    Downloads/readystamp-main/sample_data)

A. Loose files (folder 1)
   1. Open ReadyStamp and go to step 1, "Load".
   2. Stay on the "Loose files" tab.
   3. Click "Choose a folder" and pick sample_data/1_loose_files
      (or drag that folder, or some of its files, onto the drop area).
   4. Click "Load these files". You should see 12 documents, 18 pages.

B. Production with load files (folder 2)
   1. In step 1, "Load", click the "OPT production" tab.
   2. Next to "OPT file", click "Browse..." and go to
      sample_data/2_production_with_load_file/VOL001/DATA
      and choose VOL001.opt.
   3. Next to "DAT file (optional)", click "Browse..." and choose VOL001.dat
      in the same DATA folder. The begin Bates column (BEGBATES) is
      picked for you; leave the delimiter settings as they are.
   4. Click "Load production". You should see 12 documents, 18 pages,
      with their original numbers FAKE0000001 to FAKE0000018.

C. Production with problems (folder 3)
   Same steps as B, but pick the VOL001.opt and VOL001.dat inside
   sample_data/3_production_with_problems/VOL001/DATA.
   Look at the load report to see the four problems listed above.


IDEAS FOR WHAT TO TRY
---------------------

- Put text in each of the six spots (top left, top centre, top right,
  bottom left, bottom centre, bottom right) and add a watermark, then
  check how it looks on the portrait letters, the colour invoice, the
  phone photo and the sideways dock log.
- Export once as PDF and once as single-page images, and compare.
- With folder 2, use the DAT details (for example the CONFIDENTIALITY
  column, which says CONFIDENTIAL on the contracts and term sheet) in
  your stamp text by typing {FIELD:CONFIDENTIALITY} or
  {FIELD:CUSTODIAN} into a stamp box.
