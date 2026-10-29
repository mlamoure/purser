"""The shipped default category set (DESIGN §3.2, issue #110).

The hierarchy — which categories exist and how they are grouped — is adapted
from the Plaid Personal Finance Categories taxonomy, with two deliberate
exclusions: transfer groups (in Purser a transfer is structural — one
transaction, two real-account postings, never a category) and loan payments
to tracked accounts (those are transfers too; only rent/mortgage for
UNtracked loans ship as categories).

The descriptions are Purser's own wording (issue #429), not Plaid's. Every
entry carries a one-line description written for the classifier — the AI
categorization prompt lists ``path — description`` pairs, and the same text
helps humans in the Config tree. Pure data, no I/O; the seeding lives in
migration 0012 (one-shot, skip-existing) so user deletions stick, which also
means an existing install keeps the descriptions it was seeded with.
"""

from dataclasses import dataclass

EXPENSE = "category_expense"
INCOME = "category_income"


@dataclass(frozen=True)
class CategorySpec:
    path: str
    name: str
    type: str
    description: str


# Ordered parents-first so the seeder can link parent_id as it inserts.
DEFAULT_CATEGORIES: tuple[CategorySpec, ...] = (
    # -- roots ---------------------------------------------------------------
    CategorySpec("income", "Income", INCOME, "Money you receive; parent of every income category."),
    CategorySpec(
        "expenses", "Expenses", EXPENSE, "Money you spend; parent of every expense category."
    ),
    # -- income --------------------------------------------------------------
    CategorySpec(
        "income.salary",
        "Salary & wages",
        INCOME,
        "Pay for work: payroll deposits, wages, tips, and earnings from gig or app-based jobs.",
    ),
    CategorySpec(
        "income.interest",
        "Interest",
        INCOME,
        "Interest credited to you on checking, savings, or other deposit balances.",
    ),
    CategorySpec(
        "income.dividends",
        "Dividends",
        INCOME,
        "Dividend payouts on stocks or funds held in a brokerage account.",
    ),
    CategorySpec(
        "income.retirement",
        "Retirement income",
        INCOME,
        "Pension checks, Social Security benefits, and withdrawals from an IRA, "
        "401(k), or similar plan.",
    ),
    CategorySpec(
        "income.tax_refund",
        "Tax refunds",
        INCOME,
        "Money returned by a federal or state tax authority.",
    ),
    CategorySpec(
        "income.unemployment",
        "Unemployment",
        INCOME,
        "Jobless benefits and other payments from an unemployment-insurance program.",
    ),
    CategorySpec(
        "income.other",
        "Other income",
        INCOME,
        "Income no sibling covers, for example alimony, child support, rent "
        "collected from tenants, or rebates.",
    ),
    # -- expenses: housing ----------------------------------------------------
    CategorySpec(
        "expenses.housing",
        "Housing",
        EXPENSE,
        "Paying for the place you live: rent, mortgage, and association dues.",
    ),
    CategorySpec(
        "expenses.housing.rent",
        "Rent",
        EXPENSE,
        "Rent owed to a landlord or property manager for your home.",
    ),
    CategorySpec(
        "expenses.housing.mortgage",
        "Mortgage",
        EXPENSE,
        "Home-loan payments, but only when the loan itself is not an account in "
        "Purser (a payment to a tracked loan is a transfer).",
    ),
    CategorySpec(
        "expenses.housing.hoa",
        "HOA fees",
        EXPENSE,
        "Dues and special assessments charged by a homeowners or condo association.",
    ),
    # -- expenses: utilities ---------------------------------------------------
    CategorySpec(
        "expenses.utilities",
        "Utilities",
        EXPENSE,
        "Regular bills for the services that keep a household running.",
    ),
    CategorySpec(
        "expenses.utilities.electric_gas",
        "Electric & gas",
        EXPENSE,
        "Statements from the power company or the natural-gas supplier.",
    ),
    CategorySpec(
        "expenses.utilities.water_sewer",
        "Water & sewer",
        EXPENSE,
        "Municipal water supply and sewer service charges.",
    ),
    CategorySpec(
        "expenses.utilities.internet_cable",
        "Internet & cable",
        EXPENSE,
        "Home broadband plus cable or satellite television service.",
    ),
    CategorySpec(
        "expenses.utilities.mobile_phone",
        "Mobile phone",
        EXPENSE,
        "Wireless carrier charges for smartphones and other mobile lines.",
    ),
    CategorySpec(
        "expenses.utilities.trash",
        "Trash",
        EXPENSE,
        "Garbage pickup, recycling, and waste-removal service.",
    ),
    CategorySpec(
        "expenses.utilities.other",
        "Other utilities",
        EXPENSE,
        "A household utility that none of the sibling categories covers.",
    ),
    # -- expenses: food ---------------------------------------------------------
    CategorySpec(
        "expenses.food", "Food & drink", EXPENSE, "Anything eaten or drunk, at home or out."
    ),
    CategorySpec(
        "expenses.food.groceries",
        "Groceries",
        EXPENSE,
        "Food bought to prepare at home, from supermarkets, grocers, or farmers' "
        "markets — not restaurants, takeout, or meal delivery.",
    ),
    CategorySpec(
        "expenses.food.restaurants",
        "Restaurants",
        EXPENSE,
        "Eating or drinking out: sit-down restaurants, diners, pubs and bars, and "
        "fast-food counters.",
    ),
    CategorySpec(
        "expenses.food.coffee",
        "Coffee",
        EXPENSE,
        "Coffee houses, cafes, and espresso or tea bars.",
    ),
    CategorySpec(
        "expenses.food.alcohol",
        "Alcohol",
        EXPENSE,
        "Liquor stores and bottle shops selling beer, wine, or spirits to take home.",
    ),
    CategorySpec(
        "expenses.food.delivery",
        "Food delivery",
        EXPENSE,
        "Restaurant meals brought to your door through apps like DoorDash, Uber Eats, or Grubhub.",
    ),
    # -- expenses: transport -----------------------------------------------------
    CategorySpec(
        "expenses.transport",
        "Transportation",
        EXPENSE,
        "Moving yourself around: your own vehicle, transit, and hired rides.",
    ),
    CategorySpec(
        "expenses.transport.fuel",
        "Fuel",
        EXPENSE,
        "Gasoline, diesel, and electric-vehicle charging sessions.",
    ),
    CategorySpec(
        "expenses.transport.parking",
        "Parking",
        EXPENSE,
        "Paid parking at garages, lots, and meters.",
    ),
    CategorySpec(
        "expenses.transport.tolls",
        "Tolls",
        EXPENSE,
        "Charges for using toll roads, bridges, or tunnels, including toll-tag top-ups.",
    ),
    CategorySpec(
        "expenses.transport.public_transit",
        "Public transit",
        EXPENSE,
        "Fares and passes for city buses, commuter and subway trains, and ferries.",
    ),
    CategorySpec(
        "expenses.transport.rideshare",
        "Rideshare & taxis",
        EXPENSE,
        "Hired rides and short rentals: Uber, Lyft, taxicabs, and shared bikes or scooters.",
    ),
    CategorySpec(
        "expenses.transport.auto_maintenance",
        "Auto maintenance",
        EXPENSE,
        "Keeping a car on the road: mechanic repairs, tires, oil service, washes, and tow trucks.",
    ),
    CategorySpec(
        "expenses.transport.other",
        "Other transportation",
        EXPENSE,
        "Vehicle costs no sibling covers, such as registration renewals or "
        "roadside-assistance plans.",
    ),
    # -- expenses: medical --------------------------------------------------------
    CategorySpec(
        "expenses.medical",
        "Medical",
        EXPENSE,
        "Health care for the people in the household (animal care belongs under Family).",
    ),
    CategorySpec(
        "expenses.medical.primary_care",
        "Doctor visits",
        EXPENSE,
        "Appointments with a doctor or specialist, plus urgent-care clinics.",
    ),
    CategorySpec(
        "expenses.medical.dental",
        "Dental",
        EXPENSE,
        "Dentist bills: cleanings, fillings, orthodontics, and other tooth care.",
    ),
    CategorySpec(
        "expenses.medical.vision",
        "Vision",
        EXPENSE,
        "Eye exams, prescription eyewear, and contacts.",
    ),
    CategorySpec(
        "expenses.medical.pharmacy",
        "Pharmacy",
        EXPENSE,
        "Drugstore purchases: prescriptions, over-the-counter medicine, and vitamins.",
    ),
    CategorySpec(
        "expenses.medical.other",
        "Other medical",
        EXPENSE,
        "Hospital bills, lab tests, ambulance rides, therapy, and other clinical "
        "care no sibling covers.",
    ),
    # -- expenses: insurance ---------------------------------------------------------
    CategorySpec(
        "expenses.insurance",
        "Insurance",
        EXPENSE,
        "Premiums for every kind of insurance policy.",
    ),
    CategorySpec(
        "expenses.insurance.auto",
        "Auto insurance",
        EXPENSE,
        "Premiums for car, motorcycle, or other vehicle coverage.",
    ),
    CategorySpec(
        "expenses.insurance.home",
        "Home insurance",
        EXPENSE,
        "Premiums for a homeowners or renters policy.",
    ),
    CategorySpec(
        "expenses.insurance.health",
        "Health insurance",
        EXPENSE,
        "Medical, dental, or vision coverage you pay for yourself rather than through payroll.",
    ),
    CategorySpec(
        "expenses.insurance.life",
        "Life insurance",
        EXPENSE,
        "Premiums for life or disability coverage.",
    ),
    CategorySpec(
        "expenses.insurance.other",
        "Other insurance",
        EXPENSE,
        "Umbrella, pet, travel, or any other policy without its own sibling.",
    ),
    # -- expenses: shopping ------------------------------------------------------------
    CategorySpec(
        "expenses.shopping",
        "Shopping",
        EXPENSE,
        "Buying physical goods in stores or online.",
    ),
    CategorySpec(
        "expenses.shopping.clothing",
        "Clothing",
        EXPENSE,
        "Clothes, footwear, and fashion accessories.",
    ),
    CategorySpec(
        "expenses.shopping.electronics",
        "Electronics",
        EXPENSE,
        "Computers, phones, gadgets, and other consumer tech.",
    ),
    CategorySpec(
        "expenses.shopping.home_goods",
        "Home goods",
        EXPENSE,
        "Furnishings: furniture, mattresses and linens, cookware, and decor.",
    ),
    CategorySpec(
        "expenses.shopping.online_marketplaces",
        "Online marketplaces",
        EXPENSE,
        "Orders from Amazon, eBay, Etsy, or another sell-everything site when "
        "what was bought is unknown.",
    ),
    CategorySpec(
        "expenses.shopping.books",
        "Books & news",
        EXPENSE,
        "Reading material: printed or electronic books, periodicals, and newspapers.",
    ),
    CategorySpec(
        "expenses.shopping.sporting_goods",
        "Sporting goods",
        EXPENSE,
        "Gear for sports, hiking, camping, and other outdoor pursuits.",
    ),
    CategorySpec(
        "expenses.shopping.office_supplies",
        "Office supplies",
        EXPENSE,
        "Stationery, printer ink and paper, and school supplies.",
    ),
    CategorySpec(
        "expenses.shopping.gifts",
        "Gifts",
        EXPENSE,
        "Presents bought for someone else, plus greeting cards and flowers.",
    ),
    CategorySpec(
        "expenses.shopping.other",
        "Other shopping",
        EXPENSE,
        "Toys, craft materials, hobby items, and any retail no sibling covers.",
    ),
    # -- expenses: home -------------------------------------------------------------------
    CategorySpec(
        "expenses.home",
        "Home & garden",
        EXPENSE,
        "Upkeep and upgrades for the house and yard.",
    ),
    CategorySpec(
        "expenses.home.improvement",
        "Home improvement",
        EXPENSE,
        "Renovation projects and their materials: lumber, fixtures, paint, and "
        "purchases at a hardware store.",
    ),
    CategorySpec(
        "expenses.home.maintenance",
        "Home maintenance",
        EXPENSE,
        "Hired upkeep: plumbers, electricians, roofers, landscaping and yard care, "
        "and exterminators.",
    ),
    CategorySpec(
        "expenses.home.security",
        "Home security",
        EXPENSE,
        "Alarm and camera systems and the fees for monitoring them.",
    ),
    CategorySpec(
        "expenses.home.cleaning",
        "Cleaning",
        EXPENSE,
        "Paid housekeeping and house-cleaning crews.",
    ),
    # -- expenses: entertainment ------------------------------------------------------------
    CategorySpec(
        "expenses.entertainment",
        "Entertainment",
        EXPENSE,
        "Leisure spending: media, outings, games, and hobbies.",
    ),
    CategorySpec(
        "expenses.entertainment.streaming",
        "Streaming services",
        EXPENSE,
        "Subscriptions to on-demand video or music, for example Netflix or Spotify.",
    ),
    CategorySpec(
        "expenses.entertainment.events",
        "Events & attractions",
        EXPENSE,
        "Tickets and admission: concerts, live sports, cinemas, museums, and theme parks.",
    ),
    CategorySpec(
        "expenses.entertainment.games",
        "Games",
        EXPENSE,
        "Video games and in-game purchases, plus board and tabletop games.",
    ),
    CategorySpec(
        "expenses.entertainment.hobbies",
        "Hobbies",
        EXPENSE,
        "Supplies for a pastime, and dues to a club built around one.",
    ),
    CategorySpec(
        "expenses.entertainment.other",
        "Other entertainment",
        EXPENSE,
        "Nightclubs, casinos and betting, and any other leisure no sibling covers.",
    ),
    # -- expenses: personal --------------------------------------------------------------------
    CategorySpec(
        "expenses.personal",
        "Personal care",
        EXPENSE,
        "Looking after your own body and well-being.",
    ),
    CategorySpec(
        "expenses.personal.fitness",
        "Fitness",
        EXPENSE,
        "Gym memberships, exercise classes, and training apps.",
    ),
    CategorySpec(
        "expenses.personal.hair_beauty",
        "Hair & beauty",
        EXPENSE,
        "Haircuts, salon and spa visits, massage, and cosmetics.",
    ),
    CategorySpec(
        "expenses.personal.laundry",
        "Laundry",
        EXPENSE,
        "Coin laundries, dry cleaners, and drop-off laundry service.",
    ),
    CategorySpec(
        "expenses.personal.other",
        "Other personal care",
        EXPENSE,
        "Mental-health and wellness apps, counseling, and other self-care no sibling covers.",
    ),
    # -- expenses: family ---------------------------------------------------------------------
    CategorySpec(
        "expenses.family",
        "Family",
        EXPENSE,
        "Costs of children, other dependents, and pets.",
    ),
    CategorySpec(
        "expenses.family.childcare",
        "Childcare",
        EXPENSE,
        "Daycare, nannies, and babysitting.",
    ),
    CategorySpec(
        "expenses.family.education",
        "Education",
        EXPENSE,
        "Tuition and school fees, classes and courses, and textbooks.",
    ),
    CategorySpec(
        "expenses.family.kids",
        "Kids",
        EXPENSE,
        "Children's activities, gear, and allowances that are not childcare or schooling.",
    ),
    CategorySpec(
        "expenses.family.pets",
        "Pets",
        EXPENSE,
        "Food, supplies, grooming, and boarding for animals in the household.",
    ),
    CategorySpec(
        "expenses.family.veterinary",
        "Veterinary",
        EXPENSE,
        "Vet appointments, treatment, and medicine for pets.",
    ),
    # -- expenses: travel ------------------------------------------------------------------------
    CategorySpec(
        "expenses.travel",
        "Travel",
        EXPENSE,
        "Spending on trips away from home.",
    ),
    CategorySpec(
        "expenses.travel.flights",
        "Flights",
        EXPENSE,
        "Plane tickets, plus airline charges such as baggage or seat fees.",
    ),
    CategorySpec(
        "expenses.travel.lodging",
        "Lodging",
        EXPENSE,
        "Places to stay on a trip: hotels, motels, and short-term rentals like Airbnb.",
    ),
    CategorySpec(
        "expenses.travel.rental_car",
        "Rental cars",
        EXPENSE,
        "Car hire for a trip, and chartered coaches.",
    ),
    CategorySpec(
        "expenses.travel.other",
        "Other travel",
        EXPENSE,
        "Trip costs no sibling covers: cruises, travel agents, visas, and the like.",
    ),
    # -- expenses: services ------------------------------------------------------------------------
    CategorySpec(
        "expenses.services",
        "Services",
        EXPENSE,
        "Paying professionals and other service providers.",
    ),
    CategorySpec(
        "expenses.services.financial",
        "Financial services",
        EXPENSE,
        "Tax preparers, accountants, and financial advisors.",
    ),
    CategorySpec(
        "expenses.services.legal",
        "Legal",
        EXPENSE,
        "Attorney fees and other paid legal help.",
    ),
    CategorySpec(
        "expenses.services.shipping",
        "Postage & shipping",
        EXPENSE,
        "Stamps, courier and parcel charges, and packing supplies.",
    ),
    CategorySpec(
        "expenses.services.storage",
        "Storage",
        EXPENSE,
        "Self-storage units and other rented space for belongings.",
    ),
    CategorySpec(
        "expenses.services.subscriptions",
        "Software & memberships",
        EXPENSE,
        "Recurring software, cloud, and membership fees that are not entertainment.",
    ),
    CategorySpec(
        "expenses.services.other",
        "Other services",
        EXPENSE,
        "A service bill that none of the sibling categories covers.",
    ),
    # -- expenses: fees ---------------------------------------------------------------------------
    CategorySpec(
        "expenses.fees",
        "Fees & charges",
        EXPENSE,
        "Charges levied by banks, card issuers, and lenders.",
    ),
    CategorySpec(
        "expenses.fees.bank",
        "Bank fees",
        EXPENSE,
        "Bank account charges: cash-machine, overdraft, and monthly maintenance fees.",
    ),
    CategorySpec(
        "expenses.fees.interest",
        "Interest charges",
        EXPENSE,
        "Interest you are charged on a credit card balance or a loan.",
    ),
    CategorySpec(
        "expenses.fees.other",
        "Other fees",
        EXPENSE,
        "Late-payment and currency-conversion fees, and any other charge no sibling covers.",
    ),
    # -- expenses: government ------------------------------------------------
    CategorySpec(
        "expenses.government",
        "Government",
        EXPENSE,
        "Taxes and payments to public agencies.",
    ),
    CategorySpec(
        "expenses.government.taxes",
        "Tax payments",
        EXPENSE,
        "Income or property tax owed to a federal, state, or local government.",
    ),
    CategorySpec(
        "expenses.government.fees",
        "Government fees",
        EXPENSE,
        "Charges from public offices: DMV, licenses, passports, and registrations.",
    ),
    # -- expenses: giving ----------------------------------------------------
    CategorySpec(
        "expenses.giving",
        "Giving",
        EXPENSE,
        "Money given to causes and organizations.",
    ),
    CategorySpec(
        "expenses.giving.charity",
        "Charity",
        EXPENSE,
        "Gifts to charities and other nonprofit organizations.",
    ),
    CategorySpec(
        "expenses.giving.political_religious",
        "Political & religious",
        EXPENSE,
        "Contributions to political campaigns or to a place of worship.",
    ),
)
