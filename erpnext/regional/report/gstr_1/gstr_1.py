# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe, json
from frappe import _

def execute(filters=None):
	return Gstr1Report(filters).run()

class Gstr1Report(object):
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.customer_type = "Company" if self.filters.get("type_of_business") ==  "B2B" else "Individual"
		
	def run(self):
		self.get_columns()
		self.get_data()
		return self.columns, self.data

	def get_data(self):
		self.data = []
		self.get_gst_accounts()
		self.get_invoice_data()

		if not self.invoices: return

		self.get_invoice_items()
		self.get_items_based_on_tax_rate()
		invoice_fields = [d["fieldname"] for d in self.invoice_columns]


		for inv, items_based_on_rate in self.items_based_on_tax_rate.items():
			invoice_details = self.invoices.get(inv)
			for rate, items in items_based_on_rate.items():
				row = []
				for fieldname in invoice_fields:
					if fieldname == "invoice_value":
						row.append(invoice_details.base_rounded_total or invoice_details.base_grand_total)
					else:
						row.append(invoice_details.get(fieldname))

				row += [rate,
					sum([net_amount for item_code, net_amount in self.invoice_items.get(inv).items()
						if item_code in items]),
					self.invoice_cess.get(inv)
				]

				if self.filters.get("type_of_business") ==  "B2C Small":
					row.append("E" if invoice_details.ecommerce_gstin else "OE")

				self.data.append(row)

	def get_invoice_data(self):
		self.invoices = frappe._dict()
		conditions = self.get_conditions()

		invoice_data = frappe.db.sql("""
			select
				name as invoice_number,
				customer_name,
				posting_date,
				base_grand_total,
				base_rounded_total,
				customer_gstin,
				place_of_supply,
				ecommerce_gstin,
				reverse_charge,
				invoice_type
			from `tabSales Invoice`
			where docstatus = 1 %s
			order by posting_date desc
			""" % (conditions), self.filters, as_dict=1)

		for d in invoice_data:
			self.invoices.setdefault(d.invoice_number, d)

	def get_conditions(self):
		conditions = ""

		for opts in (("company", " and company=%(company)s"),
			("from_date", " and posting_date>=%(from_date)s"),
			("to_date", " and posting_date<=%(to_date)s")):
				if self.filters.get(opts[0]):
					conditions += opts[1]

		customers = frappe.get_all("Customer", filters={"customer_type": self.customer_type})
		conditions += " and customer in ('{0}')".format("', '".join([frappe.db.escape(c.name)
			for c in customers]))

		if self.filters.get("type_of_business") ==  "B2C Large":
			conditions += """ and SUBSTR(place_of_supply, 1, 2) != SUBSTR(company_gstin, 1, 2)
				and grand_total > 250000"""
		elif self.filters.get("type_of_business") ==  "B2C Small":
			conditions += """ and (
				SUBSTR(place_of_supply, 1, 2) = SUBSTR(company_gstin, 1, 2)
				or grand_total <= 250000
			)"""

		return conditions

	def get_invoice_items(self):
		self.invoice_items = frappe._dict()
		items = frappe.db.sql("""
			select item_code, parent, base_net_amount
			from `tabSales Invoice Item`
			where parent in (%s)
		""" % (', '.join(['%s']*len(self.invoices))), tuple(self.invoices), as_dict=1)

		for d in items:
			self.invoice_items.setdefault(d.parent, {}).setdefault(d.item_code, d.base_net_amount)

	def get_items_based_on_tax_rate(self):
		tax_details = frappe.db.sql("""
			select
				parent, account_head, item_wise_tax_detail, base_tax_amount_after_discount_amount
			from `tabSales Taxes and Charges`
			where
				parenttype = 'Sales Invoice' and docstatus = 1
				and parent in (%s)
				and tax_amount_after_discount_amount > 0
			order by account_head
		""" % (', '.join(['%s']*len(self.invoices.keys()))), tuple(self.invoices.keys()))

		self.items_based_on_tax_rate = {}
		self.invoice_cess = frappe._dict()
		unidentified_gst_accounts = []

		for parent, account, item_wise_tax_detail, tax_amount in tax_details:
			if account in self.gst_accounts.cess_account:
				self.invoice_cess.setdefault(parent, tax_amount)
			else:
				if item_wise_tax_detail:
					try:
						item_wise_tax_detail = json.loads(item_wise_tax_detail)
						cgst_or_sgst = False
						if account in self.gst_accounts.cgst_account \
							or account in self.gst_accounts.sgst_account:
							cgst_or_sgst = True

						if not (cgst_or_sgst or account in self.gst_accounts.igst_account):
							if "gst" in account.lower() and account not in unidentified_gst_accounts:
								unidentified_gst_accounts.append(account)
							continue

						for item_code, tax_amounts in item_wise_tax_detail.items():
							tax_rate = tax_amounts[0]
							if cgst_or_sgst:
								tax_rate *= 2

							rate_based_dict = self.items_based_on_tax_rate.setdefault(parent, {})\
								.setdefault(tax_rate, [])
							if item_code not in rate_based_dict:
								rate_based_dict.append(item_code)

					except ValueError:
						continue
		if unidentified_gst_accounts:
			frappe.msgprint(_("Following accounts might be selected in GST Settings:")
				+ "<br>" + "<br>".join(unidentified_gst_accounts), alert=True)

	def get_gst_accounts(self):
		self.gst_accounts = frappe._dict()
		gst_settings_accounts = frappe.get_list("GST Account",
			filters={"parent": "GST Settings", "company": self.filters.company},
			fields=["cgst_account", "sgst_account", "igst_account", "cess_account"])

		if not gst_settings_accounts:
			frappe.throw(_("Please set GST Accounts in GST Settings"))

		for d in gst_settings_accounts:
			for acc, val in d.items():
				self.gst_accounts.setdefault(acc, []).append(val)

	def get_columns(self):
		self.tax_columns = [
			{
				"fieldname": "rate",
				"label": "Rate",
				"fieldtype": "Int",
				"width": 60
			},
			{
				"fieldname": "taxable_value",
				"label": "Taxable Value",
				"fieldtype": "Currency",
				"width": 100
			},
			{
				"fieldname": "cess_amount",
				"label": "Cess Amount",
				"fieldtype": "Currency",
				"width": 100
			}
		]
		self.other_columns = []

		if self.filters.get("type_of_business") ==  "B2B":
			self.invoice_columns = [
				{
					"fieldname": "customer_gstin",
					"label": "GSTIN/UIN of Recipient",
					"fieldtype": "Data"
				},
				{
					"fieldname": "customer_name",
					"label": "Receiver Name",
					"fieldtype": "Data"
				},
				{
					"fieldname": "invoice_number",
					"label": "Invoice Number",
					"fieldtype": "Link",
					"options": "Sales Invoice"
				},
				{
					"fieldname": "posting_date",
					"label": "Invoice date",
					"fieldtype": "Date"
				},
				{
					"fieldname": "invoice_value",
					"label": "Invoice Value",
					"fieldtype": "Currency"
				},
				{
					"fieldname": "place_of_supply",
					"label": "Place of Supply",
					"fieldtype": "Data"
				},
				{
					"fieldname": "reverse_charge",
					"label": "Reverse Charge",
					"fieldtype": "Data"
				},
				{
					"fieldname": "invoice_type",
					"label": "Invoice Type",
					"fieldtype": "Data"
				},
				{
					"fieldname": "ecommerce_gstin",
					"label": "E-Commerce GSTIN",
					"fieldtype": "Data"
				}
			]
		elif self.filters.get("type_of_business") ==  "B2C Large":
			self.invoice_columns = [
				{
					"fieldname": "invoice_number",
					"label": "Invoice Number",
					"fieldtype": "Link",
					"options": "Sales Invoice",
					"width": 120
				},
				{
					"fieldname": "posting_date",
					"label": "Invoice date",
					"fieldtype": "Date",
					"width": 100
				},
				{
					"fieldname": "invoice_value",
					"label": "Invoice Value",
					"fieldtype": "Currency",
					"width": 100
				},
				{
					"fieldname": "place_of_supply",
					"label": "Place of Supply",
					"fieldtype": "Data",
					"width": 120
				},
				{
					"fieldname": "ecommerce_gstin",
					"label": "E-Commerce GSTIN",
					"fieldtype": "Data",
					"width": 130
				}
			]
		elif self.filters.get("type_of_business") ==  "B2C Small":
			self.invoice_columns = [
				{
					"fieldname": "place_of_supply",
					"label": "Place of Supply",
					"fieldtype": "Data",
					"width": 120
				},
				{
					"fieldname": "ecommerce_gstin",
					"label": "E-Commerce GSTIN",
					"fieldtype": "Data",
					"width": 130
				}
			]
			self.other_columns = [
				{
					"fieldname": "type",
					"label": "Type",
					"fieldtype": "Data",
					"width": 50
				}
			]
		self.columns = self.invoice_columns + self.tax_columns + self.other_columns