# Amazon Invoice Coding Dashboard

Project requirements are listed in 'Monarch - Junior Application Developer Interview Task.pdf'. This project provides a solution for Amazon invoice billing to GL account matching.

**Stack:** Python 3.14 · Django 5.1 · SQLite · [pdfplumber](https://github.com/jsvine/pdfplumber) · [sentence-transformers](https://sbert.net) ([all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)) · [numpy](https://numpy.org)

---

## What the App Does
Using a PDF parsing and ML scoring model, this application aids users in efficiently and accurately mapping invoice totals to their respective properties and GL accounts. Users follow a flow: Upload -> Audit -> Approve -> Verify/Reporting -> Submit

This flow is built to allow automation where available but with human approval as the final say. 

---

## How to Run It

```
# Initial setup
make setup
make cache-model

# Regular use
make run

```

## Architecture and Design Decisions

### Why Python/Django

- With a short timeline, it was great for spinning up a full stack application with many libraries available to complete the task
- Python's ecosystem assisted with much of the heavy lifting for the language model
- Django seems to be main framework used at Monarch, thus a good fit for this challenge


### Why use a language model without automation

This project easily had the potential to fully automate the mapping of invoice line items to GL/property accounts. However, while a few line items may not be critical if they landed in the wrong GL account, I made a few assumptions that this would be a recurring process and could have a growing pain of misidentifying accounts. The value would be lost on the cleanup. With the human approval/model training system in place, this fronts some manual work to train the model while paying out efficiency and accuracy later on. With my history of GL accounts, I know they are not always as obvious as they might seem to know what goes where. Certainly too much for a simple language model to make the final say.

---

## Assumptions

- The end user is not very technically inclined
- The end user would be familiar with the GL accounts and/or be in contact with the purchaser for any needed follow-ups
- The volume of invoices would be somewhere between 10-500 monthly
- The GL/Property codes can change over time
- Yardi is the final destination for all invoice data
- Backup for audit purposes will be needed
- All invoices will have an Invoice # and a Property Code and possibly a GL code
- The invoice level GL code is always correct for a single line item
- The invoice level GL code is where all discount and shipping line items will be assigned
- The invoice level GL code is likely correct for most line items
- GL codes 6000-7070 are most applicable to invoice mapping


---

## Known Limitations

This is not a production ready project. There is not much support for stepping back in the work flow, particularly for the end user. The model can claim some bad confidence levels (curious to spend more time training model to see how it would handle). Moving some of the built-in scripts for the end user to access might be nice. Duplicate invoices can be an issue. Adding/removing GL codes can be an issue.

---

## What I Would Build or Change for Production

Fine-tune the model config values for better accuracy and train it with accurate GL categorization. Add some amount of user recognition that would allow the final audit file to reflect accurately for audit-trail purposes. More robust, more testing, more error handling. Move the developer scripts into the project to allow for some end user control. Convert some scripts into more fine-tuned interactions on the site, such as allowing users to remove a single invoice from the batch. Consider other models to weigh processing to results. Depending on model success, automatically move some line items to complete based on high confidence levels. Unrelated to the project directly, but I would also heavily push for process changes outside of the project to help too -- Providing multiple GL codes on the invoice or requiring all invoices to have a GL Code. 