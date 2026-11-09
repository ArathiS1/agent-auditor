"""A small, fictional shop help center ("Northwind Outfitters") with a question set that has known right answers.

Each fact has a value that appears in the passage text. A question is answered correctly when that value appears in
the agent's answer. A "stale" copy of every fact (a different value) is used to plant a fault that retrieval and the
built-in scorers cannot see: the agent finds the right passage, but the passage is out of date.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# (id, passage template with {v}, correct value, stale value, [questions])
FACTS = [
    ("return_window", "Unworn items can be returned within {v} of delivery for a full refund.", "30 days", "14 days",
     ["How long do I have to return an item?", "What is the return window for unworn items?",
      "Within how many days of delivery can I get a refund on an unworn item?", "How many days after delivery can I send something back?"]),
    ("return_shipping", "Return shipping is free for orders over {v}; smaller orders pay a flat return label fee.", "$75", "$50",
     ["When is return shipping free?", "How much must an order be for free return shipping?",
      "What order total makes return shipping free?", "Do I pay to ship a return back, and above what order amount is it free?"]),
    ("standard_shipping", "Standard shipping takes {v} business days within the continental US.", "3 to 5", "7 to 10",
     ["How long does standard shipping take?", "How many business days does standard shipping take in the continental US?",
      "When will a standard shipped order arrive?", "What is the delivery time for standard shipping?"]),
    ("express_shipping", "Express shipping costs {v} and arrives in two business days.", "$18", "$25",
     ["How much does express shipping cost?", "What is the price of express delivery?",
      "What do I pay for two day express shipping?", "How much is express shipping and how fast is it?"]),
    ("free_shipping", "Standard shipping is free on orders over {v}.", "$60", "$100",
     ["What order total gets free standard shipping?", "How much do I need to spend for free shipping?",
      "Above what amount is standard shipping free?", "What is the free shipping minimum?"]),
    ("international", "We ship to {v} countries outside the US, and the buyer pays any customs fees.", "38", "12",
     ["How many countries do you ship to outside the US?", "Do you ship internationally and to how many countries?",
      "Who pays customs fees on international orders and how many countries are covered?", "How many international destinations can you ship to?"]),
    ("tent_warranty", "Every tent carries a {v} warranty against manufacturing defects.", "5-year", "1-year",
     ["How long is the warranty on tents?", "What warranty do tents have against manufacturing defects?",
      "How many years of warranty does a tent come with?", "Are tents covered for defects and for how long?"]),
    ("pack_repair", "Backpacks are covered by a lifetime repair program with a {v} inspection fee.", "$10", "$40",
     ["What is the inspection fee for the backpack repair program?", "How much does a backpack repair inspection cost?",
      "Is there a fee to inspect a backpack under the lifetime repair program?", "What do I pay for the lifetime backpack repair inspection?"]),
    ("weekday_hours", "The Portland store is open {v} on weekdays.", "9am to 7pm", "10am to 6pm",
     ["What are the Portland store hours on weekdays?", "When does the Portland store open and close during the week?",
      "What time is the Portland store open on weekdays?", "What are your weekday opening hours in Portland?"]),
    ("saturday_hours", "On Saturdays the Portland store is open {v}.", "10am to 5pm", "noon to 4pm",
     ["What are the Portland store hours on Saturday?", "When is the Portland store open on Saturdays?",
      "What time does the Portland store open on Saturday?", "Saturday opening hours for the Portland store?"]),
    ("gift_cards", "Gift cards never expire and are sold in amounts from {v}.", "$10 to $500", "$25 to $200",
     ["What amounts do gift cards come in?", "Do gift cards expire and what values can I buy?",
      "What is the range of gift card values?", "How much can a gift card be for?"]),
    ("price_match", "We match any competitor's lower price within {v} of purchase.", "14 days", "48 hours",
     ["How long after purchase can I ask for a price match?", "What is the price match window?",
      "Within how many days of buying will you match a lower competitor price?", "Do you price match and for how long after I buy?"]),
    ("rewards_rate", "Trailhead Rewards members earn {v} points for every dollar spent.", "2", "1",
     ["How many points do rewards members earn per dollar?", "How fast do Trailhead Rewards points build up?",
      "How many Trailhead Rewards points per dollar spent?", "What is the points earning rate for rewards members?"]),
    ("rewards_redeem", "Every {v} points can be redeemed for a $5 reward.", "100", "250",
     ["How many points do I need for a $5 reward?", "How many Trailhead points make a $5 reward?",
      "What number of points can be redeemed for five dollars?", "How many rewards points equal a $5 reward?"]),
    ("order_cancel", "Orders can be cancelled free of charge within {v} of being placed.", "2 hours", "30 minutes",
     ["How long do I have to cancel an order for free?", "Within what time can I cancel my order without a fee?",
      "Until when can I cancel an order free of charge?", "What is the free cancellation period after placing an order?"]),
    ("tracking", "A tracking number is emailed within {v} after your order ships.", "24 hours", "5 days",
     ["When will I get my tracking number?", "How soon after shipping is the tracking number emailed?",
      "How long until I receive tracking information for my order?", "Within what time is the tracking number sent once my order ships?"]),
    ("rental_price", "Camping gear rentals cost {v} per day for a two-person tent.", "$12", "$30",
     ["How much does it cost to rent a two-person tent per day?", "What is the daily rental price of a two-person tent?",
      "What do camping gear rentals cost per day for a two-person tent?", "Price per day to rent a two person tent?"]),
    ("rental_deposit", "Rentals require a refundable deposit of {v}.", "$50", "$150",
     ["How much is the rental deposit?", "Is there a refundable deposit for rentals and how much?",
      "What deposit do I pay to rent gear?", "How large is the refundable rental deposit?"]),
    ("rental_late", "Late rental returns incur a fee of {v} for each extra day.", "$15", "$5",
     ["What is the late fee for rentals?", "How much is charged for each extra day a rental is late?",
      "What happens if I return rented gear late and how much per day?", "What is the fee per extra day for late rental returns?"]),
    ("repair_time", "Standard repairs take about {v} to complete.", "10 business days", "6 weeks",
     ["How long do repairs take?", "How long does a standard repair take to complete?",
      "What is the turnaround time for repairs?", "How many business days for a standard repair?"]),
    ("boot_exchange", "Boots can be exchanged once for a different size within {v}.", "45 days", "10 days",
     ["How long do I have to exchange boots for another size?", "Can I swap boots for a different size and within how many days?",
      "What is the boot size exchange window?", "Within how many days can boots be exchanged for a different size?"]),
    ("group_discount", "Teams ordering {v} or more items get a 10% group discount.", "20", "100",
     ["How many items do I need to order for the group discount?", "What is the minimum for a team group discount?",
      "How many items must a team order to get 10% off?", "At what quantity does the 10% group discount start?"]),
    ("student_discount", "Students receive a {v} discount with a valid .edu email address.", "15%", "5%",
     ["How much is the student discount?", "What percent off do students get with a .edu email?",
      "Is there a discount for students and how big is it?", "What discount do I get as a student with a valid edu email?"]),
    ("military_discount", "Military members receive a {v} discount in store.", "10%", "25%",
     ["How much is the military discount?", "What percent discount do military members get in store?",
      "Do you offer a military discount and how much?", "What is the in store discount for military members?"]),
    ("payment_plans", "We accept Visa, Mastercard, PayPal and {v} installment plans.", "Affirm", "Klarna",
     ["Which installment plan provider do you accept?", "Do you accept installment plans and with which company?",
      "What payment methods do you take besides cards?", "Which buy now pay later service do you use?"]),
    ("installments", "Orders over $200 can be split into {v} interest-free payments.", "4", "12",
     ["Into how many payments can I split an order over $200?", "How many interest-free payments are available?",
      "Can I pay in installments for a large order and how many?", "How many interest-free installments for orders over $200?"]),
    ("support_hours", "Phone support is available {v}, Monday to Friday.", "8am to 8pm Eastern", "9am to 5pm Pacific",
     ["What are the phone support hours?", "When can I call customer support?",
      "What hours is phone support open Monday to Friday?", "Phone support hours during the week?"]),
    ("support_phone", "Call customer support at {v}.", "1-800-555-0142", "1-800-555-0199",
     ["What is the customer support phone number?", "Which number do I call for customer support?",
      "How do I phone customer support?", "What number reaches support by phone?"]),
    ("packaging", "Recycled-material products carry a green tag, and {v} of our packaging is plastic-free.", "90%", "40%",
     ["How much of your packaging is plastic-free?", "What share of packaging is free of plastic?",
      "What percent of Northwind packaging is plastic-free?", "What does the green tag mean and how much packaging is plastic-free?"]),
    ("preorders", "Pre-orders are charged when the item ships, with an estimated wait of {v}.", "6 weeks", "3 months",
     ["How long is the wait for a pre-order?", "When am I charged for a pre-order and how long is the wait?",
      "What is the estimated wait for pre-ordered items?", "How long do I wait on a pre-order?"]),
]

OUT_OF_SCOPE = [
    "Do you sell kayaks?", "Can you recommend a good hiking trail in Oregon?", "What is the weather in Portland today?",
    "Do you have a store in Chicago?", "Can I buy a firearm from you?", "How do I change my password?",
    "Do you hire seasonal staff?", "What is your CEO's name?", "Can you ship to a PO box on the moon?", "Do you sell fishing licenses?",
]

DECLINE = "I could not find that in our help center."
_STOP = frozenset("a an and are as at be but by can do does for from how i if in is it me my of on or our so than that the their then there these they this to we what when where which who will with you your".split())


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    value: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    fact_id: str | None  # None = out of scope
    gold: str | None  # value that must appear in a correct answer; None = the agent should decline


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9$%]+", text.lower()):
        if w in _STOP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def passages(stale: frozenset[str] = frozenset()) -> list[Passage]:
    """The knowledge base. Ids in `stale` use the out-of-date value."""
    out = []
    for fid, template, value, stale_value, _ in FACTS:
        v = stale_value if fid in stale else value
        out.append(Passage(fid, template.replace("{v}", v), v))
    return out


def questions() -> list[Question]:
    qs = [Question(f"{fid}-{i + 1}", q, fid, value) for fid, _t, value, _s, qlist in FACTS for i, q in enumerate(qlist)]
    qs += [Question(f"oos-{i + 1}", q, None, None) for i, q in enumerate(OUT_OF_SCOPE)]
    return qs


class Index:
    """TF-IDF cosine search over a set of passages. Scores are scaled by SCORE_SCALE so a good match is near 0.6-1.0
    (the built-in retrieval_top_score scorer expects a 0..1 relevance and flags anything under 0.5)."""

    SCORE_SCALE = 1.6

    def __init__(self, docs: list[Passage]) -> None:
        self.docs = docs
        self.tf = [Counter(tokens(d.text)) for d in docs]
        df = Counter(t for c in self.tf for t in c)
        n = len(docs)
        self.idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
        self.norm = [math.sqrt(sum((c[t] * self.idf[t]) ** 2 for t in c)) or 1.0 for c in self.tf]

    def search(self, query: str, k: int = 3) -> list[tuple[Passage, float]]:
        q = Counter(t for t in tokens(query) if t in self.idf)
        if not q:
            return []
        qn = math.sqrt(sum((c * self.idf[t]) ** 2 for t, c in q.items())) or 1.0
        scored = []
        for doc, tf, dn in zip(self.docs, self.tf, self.norm):
            dot = sum(c * self.idf[t] * tf[t] * self.idf[t] for t, c in q.items() if t in tf)
            if dot > 0:
                scored.append((doc, min(1.0, self.SCORE_SCALE * dot / (qn * dn))))
        scored.sort(key=lambda p: -p[1])
        return scored[:k]
